import os

from collections.abc import Callable, Sequence
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, Self
from fastapi import APIRouter, Body, HTTPException, Path, status, Query
from kink import di
from loguru import logger
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, object_session

from program.db import db_functions
from program.db.db import db_session
from program.media.collection import CollectionEntry
from program.media.filesystem_entry import FilesystemEntry
from program.media.item import Episode, MediaItem, Movie, Season, Show
from program.media.models import ActiveStream
from program.media.stream import Stream
from program.media.state import States
from program.settings import settings_manager
from program.types import Event
from program.program import Program
from program.media.models import MediaMetadata
from program.utils.time import to_iso_utc, utcnow

from ..models.shared import IdListPayload, MessageResponse


class MediaTypeEnum(str, Enum):
    MOVIE = "movie"
    SHOW = "show"
    SEASON = "season"
    EPISODE = "episode"
    ANIME = "anime"


class SortOrderEnum(str, Enum):
    TITLE_ASC = "title_asc"
    TITLE_DESC = "title_desc"
    DATE_ASC = "date_asc"
    DATE_DESC = "date_desc"

    @property
    def sort_type(self) -> str:
        return "title" if self.value.startswith("title") else "date"


# How many candidate releases a detail view gets by rank. Releases that were
# tried, pinned or downloaded are always shown on top of this.
STREAM_LIST_LIMIT = 25

router = APIRouter(
    prefix="/items",
    tags=["items"],
    responses={404: {"description": "Not found"}},
)


def handle_ids(ids: Sequence[str | int]) -> list[int]:
    try:
        id_list = [int(id) for id in ids]

        if not id_list:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No item ID provided",
            )

        return id_list
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid item ID(s) provided",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error processing item ID(s): {str(e)}",
        ) from e


# Convenience helper to mutate an item and update states consistently
def apply_item_mutation(
    program: Program,
    session: Session,
    item: MediaItem,
    mutation_fn: "Callable[[MediaItem, Session], None]",
    bubble_parents: bool = True,
) -> None:
    """Cancel jobs, apply mutation, then update item and ancestor states.
    - Uses base MediaItem.store_state to avoid recursive child updates for seasons/shows.
    - Caller is responsible for session.commit().
    """

    try:
        program.em.cancel_job(item.id)
    except Exception:
        logger.debug(f"No active job to cancel for item {getattr(item, 'id', None)}")

    # Ensure attached instance
    if object_session(item) is not session:
        item = session.merge(item)

    # Apply mutation
    mutation_fn(item, session)

    # Update self state (non-recursive)
    try:
        MediaItem.store_state(item)
    except Exception as e:
        logger.warning(f"Failed to store state for {item.id}: {e}")

    if not bubble_parents:
        return

    # Update parent states (non-recursive)
    try:
        if isinstance(item, Episode):
            season = session.get(Season, item.parent_id)

            if season:
                MediaItem.store_state(season)
                show = session.get(Show, season.parent_id)

                if show:
                    MediaItem.store_state(show)
        elif isinstance(item, Season):
            show = session.get(Show, item.parent_id)

            if show:
                MediaItem.store_state(show)
    except Exception as e:
        logger.warning(f"Failed to update parent state(s) for item {item.id}: {e}")


class StateResponse(BaseModel):
    success: Annotated[
        bool,
        Field(description="Boolean signifying whether the request was successful"),
    ]
    states: Annotated[
        list[str],
        Field(description="The list of states"),
    ]


@router.get(
    "/states",
    operation_id="get_states",
    response_model=StateResponse,
)
async def get_states() -> StateResponse:
    return StateResponse(states=[state._name_ for state in States], success=True)


class ItemsResponse(BaseModel):
    success: Annotated[
        bool,
        Field(description="Boolean signifying whether the request was successful"),
    ]
    items: Annotated[
        list[dict[str, Any]],
        Field(description="The list of media items"),
    ]
    page: Annotated[
        int,
        Field(description="Current page number"),
    ]
    limit: Annotated[
        int,
        Field(description="Number of items per page"),
    ]
    total_items: Annotated[
        int,
        Field(description="Total number of items"),
    ]
    total_pages: Annotated[
        int,
        Field(description="Total number of pages"),
    ]


class StatesFilter(str, Enum):
    All = "All"


@router.get(
    "",
    summary="Search Media Items",
    description="Fetch media items with optional filters and pagination",
    operation_id="get_items",
    response_model=ItemsResponse,
)
async def get_items(
    limit: Annotated[
        int,
        Query(
            description="Number of items per page",
            ge=1,
        ),
    ] = 50,
    page: Annotated[
        int,
        Query(
            description="Page number",
            ge=1,
        ),
    ] = 1,
    type: Annotated[
        list[MediaTypeEnum] | None,
        Query(description="Filter by media type(s)"),
    ] = None,
    states: Annotated[
        list[States | StatesFilter] | None,
        Query(description="Filter by state(s)"),
    ] = None,
    sort: Annotated[
        list[SortOrderEnum] | None,
        Query(
            description="Sort order(s). Multiple sorts allowed but only one per type (title or date)"
        ),
    ] = None,
    search: Annotated[
        str | None,
        Query(
            description="Search by title or IMDB/TVDB/TMDB ID",
            min_length=1,
        ),
    ] = None,
    extended: Annotated[
        bool,
        Query(description="Include extended item details"),
    ] = False,
) -> ItemsResponse:
    query = select(MediaItem)

    if search:
        search_lower = search.lower()

        if search_lower.startswith("tt"):
            query = query.where(MediaItem.imdb_id == search_lower)
        elif search_lower.startswith("tmdb_"):
            tmdb_id = search_lower.replace("tmdb_", "")
            query = query.where(MediaItem.tmdb_id == tmdb_id)
        elif search_lower.startswith("tvdb_"):
            tvdb_id = search_lower.replace("tvdb_", "")
            query = query.where(MediaItem.tvdb_id == tvdb_id)
        else:
            query = query.where(func.lower(MediaItem.title).like(f"%{search_lower}%"))

    if states and StatesFilter.All not in states:
        query = query.where(
            MediaItem.last_state.in_([s for s in states if isinstance(s, States)])
        )

    if type:
        media_types = {t.value for t in type}

        if MediaTypeEnum.ANIME in type:
            media_types.remove(MediaTypeEnum.ANIME.value)

            if not media_types:
                query = query.where(MediaItem.is_anime == True)
            else:
                query = query.where(
                    and_(
                        MediaItem.type.in_(
                            media_types if media_types else ["movie", "show"]
                        ),
                        MediaItem.is_anime == True,
                    )
                )

        elif media_types:
            query = query.where(MediaItem.type.in_(media_types))

    if sort:
        # Verify we don't have multiple sorts of the same type
        sort_types = set[str]()

        for sort_criterion in sort:
            sort_type = sort_criterion.sort_type

            if sort_type in sort_types:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Multiple {sort_type} sort criteria provided. Only one sort per type is allowed.",
                )

            sort_types.add(sort_type)

        for sort_criterion in sort:
            if sort_criterion == SortOrderEnum.TITLE_ASC:
                query = query.order_by(MediaItem.title.asc())
            elif sort_criterion == SortOrderEnum.TITLE_DESC:
                query = query.order_by(MediaItem.title.desc())
            elif sort_criterion == SortOrderEnum.DATE_ASC:
                query = query.order_by(MediaItem.requested_at.asc())
            elif sort_criterion == SortOrderEnum.DATE_DESC:
                query = query.order_by(MediaItem.requested_at.desc())

    else:
        query = query.order_by(MediaItem.requested_at.desc())

    with db_session() as session:
        total_items = session.execute(
            select(func.count()).select_from(query.subquery())
        ).scalar_one()

        items = (
            session.execute(query.offset((page - 1) * limit).limit(limit))
            .unique()
            .scalars()
            .all()
        )

        total_pages = (total_items + limit - 1) // limit

        return ItemsResponse(
            success=True,
            items=[
                item.to_extended_dict() if extended else item.to_dict()
                for item in items
            ],
            page=page,
            limit=limit,
            total_items=total_items,
            total_pages=total_pages,
        )


class AddMediaItemPayload(BaseModel):
    tmdb_ids: Annotated[
        list[str] | None,
        Field(
            default=None,
            description="Comma-separated list of TMDB IDs",
        ),
    ]
    tvdb_ids: Annotated[
        list[str] | None,
        Field(
            default=None,
            description="Comma-separated list of TVDB IDs",
        ),
    ]
    tpdb_ids: Annotated[
        list[str] | None,
        Field(
            default=None,
            description="Comma-separated list of TPDB UUIDs",
        ),
    ]
    media_type: Annotated[
        Literal["movie", "tv"],
        Field(description="Media type"),
    ]


@router.post(
    "/add",
    summary="Add Media Items",
    description="""
        Add media items with bases on TMDB ID or TVDB ID,
        you can add multiple IDs by comma separating them.
    """,
    operation_id="add_items",
    response_model=MessageResponse,
)
async def add_items(
    payload: Annotated[
        AddMediaItemPayload,
        Body(description="Add media items payload"),
    ],
) -> MessageResponse:
    if not payload.tmdb_ids and not payload.tvdb_ids and not payload.tpdb_ids:
        raise HTTPException(status_code=400, detail="No ID(s) provided")

    all_tmdb_ids = (
        [id.strip() for id in payload.tmdb_ids if id]
        if payload.tmdb_ids and payload.media_type == "movie"
        else None
    )

    all_tvdb_ids = (
        [id.strip() for id in payload.tvdb_ids if id]
        if payload.tvdb_ids and payload.media_type == "tv"
        else None
    )

    all_tpdb_ids = (
        [id.strip() for id in payload.tpdb_ids if id] if payload.tpdb_ids else None
    )

    added_count = 0
    items = list[MediaItem]()

    with db_session() as session:
        if all_tmdb_ids:
            for id in all_tmdb_ids:
                # Check if item exists using ORM
                existing = session.execute(
                    select(MediaItem).where(MediaItem.tmdb_id == id)
                ).scalar_one_or_none()

                if not existing:
                    item = MediaItem(
                        {
                            "tmdb_id": id,
                            "requested_by": "riven",
                            "requested_at": utcnow(),
                        }
                    )

                    if item:
                        items.append(item)
                else:
                    logger.debug(f"Item with TMDB ID {id} already exists")

        if all_tvdb_ids:
            for id in all_tvdb_ids:
                # Check if item exists using ORM
                existing = session.execute(
                    select(MediaItem).where(MediaItem.tvdb_id == id)
                ).scalar_one_or_none()

                if not existing:
                    item = MediaItem(
                        {
                            "tvdb_id": id,
                            "requested_by": "riven",
                            "requested_at": utcnow(),
                        }
                    )
                    if item:
                        items.append(item)
                else:
                    logger.debug(f"Item with TVDB ID {id} already exists")

        if all_tpdb_ids:
            for id in all_tpdb_ids:
                existing = session.execute(
                    select(MediaItem).where(MediaItem.tpdb_id == id)
                ).scalar_one_or_none()

                if not existing:
                    item = MediaItem(
                        {
                            "tpdb_id": id,
                            "requested_by": "riven",
                            "requested_at": utcnow(),
                        }
                    )
                    if item:
                        items.append(item)
                else:
                    logger.debug(f"Item with TPDB ID {id} already exists")

        if items:
            for item in items:
                # add_item returns False when the item is deduped away; counting
                # unconditionally reported success for items that were silently
                # dropped.
                if di[Program].em.add_item(item):
                    added_count += 1
                else:
                    logger.debug(
                        f"Item {item.log_string} was not queued (already present or running)"
                    )

    return MessageResponse(message=f"Added {added_count} item(s) to the queue")


# In-flight states, in the order an item passes through them. Completed,
# Failed, Paused and Unreleased are deliberately absent: they are outcomes,
# not work in progress.
_ACTIVE_STATES: tuple[States, ...] = (
    States.Requested,
    States.Indexed,
    States.Scraped,
    States.Downloaded,
    States.Symlinked,
)


class DownloadActivityEntry(BaseModel):
    """One row of the downloads view."""

    riven_id: Annotated[int, Field(description="The Riven media item id")]
    title: Annotated[str, Field(description="Item title")]
    type: Annotated[str, Field(description="Item type")]
    state: Annotated[str, Field(description="Current state")]
    tpdb_id: Annotated[str | None, Field(description="TPDB uuid, when adult")]
    poster_path: Annotated[str | None, Field(description="Poster URL")]
    requested_at: Annotated[
        str | None, Field(description="When the item was requested, ISO 8601")
    ]
    scraped_at: Annotated[
        str | None, Field(description="When the item was last scraped, ISO 8601")
    ]
    scraped_times: Annotated[int, Field(description="How many scrape passes have run")]
    stream_count: Annotated[
        int, Field(description="Usable streams found so far")
    ]
    blacklisted_count: Annotated[
        int, Field(description="Streams rejected for this item")
    ]
    file_size: Annotated[
        int | None, Field(description="Total bytes on disk, when downloaded")
    ]
    completed_at: Annotated[
        str | None,
        Field(description="When the file appeared on disk, ISO 8601"),
    ]


class DownloadActivityResponse(BaseModel):
    active: Annotated[
        list[DownloadActivityEntry],
        Field(description="Items still moving through the pipeline, oldest request first"),
    ]
    recent: Annotated[
        list[DownloadActivityEntry],
        Field(description="Most recently completed items, newest first"),
    ]
    page: Annotated[int, Field(description="Current page number, applies to `active`")]
    limit: Annotated[int, Field(description="Rows per page, applies to `active`")]
    total_active: Annotated[
        int, Field(description="Total in-flight rows matching the filter, across all pages")
    ]
    total_pages: Annotated[int, Field(description="Total pages of `active` rows")]


def _activity_entry(item: MediaItem) -> DownloadActivityEntry:
    """Flatten one item plus its filesystem entries into a view row."""

    entries = list(item.filesystem_entries or [])
    # A title can land as several files (split scenes), so size is the sum and
    # the completion time is the last file to arrive.
    file_size = sum(entry.file_size or 0 for entry in entries) or None
    created = [entry.created_at for entry in entries if entry.created_at]
    state = item.last_state or item.state

    return DownloadActivityEntry(
        riven_id=item.id,
        title=item.title,
        type=item.type,
        state=state.name if state else States.Unknown.name,
        tpdb_id=item.tpdb_id,
        poster_path=item.poster_path,
        requested_at=to_iso_utc(item.requested_at),
        scraped_at=to_iso_utc(item.scraped_at),
        scraped_times=item.scraped_times or 0,
        stream_count=len(item.streams or []),
        blacklisted_count=len(item.blacklisted_streams or []),
        file_size=file_size,
        completed_at=to_iso_utc(max(created)) if created else None,
    )


@router.get(
    "/downloads",
    summary="Download Activity",
    description="In-flight items and recently completed downloads",
    operation_id="get_download_activity",
    response_model=DownloadActivityResponse,
)
async def get_download_activity(
    limit: Annotated[
        int,
        Query(description="Maximum rows per section", ge=1, le=100),
    ] = 25,
    page: Annotated[
        int,
        Query(description="Page number, applies to `active` only", ge=1),
    ] = 1,
    states: Annotated[
        list[States | StatesFilter] | None,
        Query(
            description="Restrict `active` to these states. Default is the "
            "usual in-flight set (Requested/Indexed/Scraped/Downloaded/"
            "Symlinked)."
        ),
    ] = None,
    search: Annotated[
        str | None,
        Query(description="Filter `active` by title, case-insensitive"),
    ] = None,
    sort: Annotated[
        list[SortOrderEnum] | None,
        Query(
            description="Sort order(s) for `active`. Multiple sorts allowed "
            "but only one per type (title or date). Defaults to oldest "
            "request first."
        ),
    ] = None,
) -> DownloadActivityResponse:
    """The two halves of "what is my downloader doing".

    Riven has no per-torrent progress to report -- a debrid provider either has
    a release or it does not -- so progress is expressed as the state an item
    has reached and how many scrape passes it has taken to get there. Items that
    have been scraped repeatedly without a usable stream are the ones actually
    stuck, and this makes that legible.

    `recent` is not paginated/filtered -- it is a fixed-size "what just
    finished" strip, not a list someone manages.
    """

    active_states = (
        [s for s in states if isinstance(s, States)]
        if states and StatesFilter.All not in states
        else list(_ACTIVE_STATES)
    ) or list(_ACTIVE_STATES)

    active_query = select(MediaItem).where(MediaItem.last_state.in_(active_states))

    if search:
        active_query = active_query.where(
            func.lower(MediaItem.title).like(f"%{search.lower()}%")
        )

    if sort:
        sort_types = set[str]()

        for sort_criterion in sort:
            sort_type = sort_criterion.sort_type

            if sort_type in sort_types:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Multiple {sort_type} sort criteria provided. Only one sort per type is allowed.",
                )

            sort_types.add(sort_type)

        for sort_criterion in sort:
            if sort_criterion == SortOrderEnum.TITLE_ASC:
                active_query = active_query.order_by(MediaItem.title.asc())
            elif sort_criterion == SortOrderEnum.TITLE_DESC:
                active_query = active_query.order_by(MediaItem.title.desc())
            elif sort_criterion == SortOrderEnum.DATE_ASC:
                active_query = active_query.order_by(MediaItem.requested_at.asc())
            elif sort_criterion == SortOrderEnum.DATE_DESC:
                active_query = active_query.order_by(MediaItem.requested_at.desc())
    else:
        # Oldest request first: whatever has been waiting longest is the
        # thing worth looking at.
        active_query = active_query.order_by(MediaItem.requested_at.asc())

    with db_session() as session:
        total_active = session.execute(
            select(func.count()).select_from(active_query.subquery())
        ).scalar_one()

        active = (
            session.execute(active_query.offset((page - 1) * limit).limit(limit))
            .unique()
            .scalars()
            .all()
        )

        # History is ordered by when the file actually landed, not when it was
        # requested -- an item queued weeks ago that only just completed
        # belongs at the top. A title can own several files, so collapse them
        # to the newest one before ordering.
        landed_at = (
            select(
                FilesystemEntry.media_item_id.label("media_item_id"),
                func.max(FilesystemEntry.created_at).label("landed_at"),
            )
            .where(FilesystemEntry.media_item_id.is_not(None))
            .group_by(FilesystemEntry.media_item_id)
            .subquery()
        )

        recent = (
            session.execute(
                select(MediaItem)
                .join(landed_at, landed_at.c.media_item_id == MediaItem.id)
                .where(MediaItem.last_state == States.Completed)
                .order_by(landed_at.c.landed_at.desc())
                .limit(limit)
            )
            .unique()
            .scalars()
            .all()
        )

        total_pages = (total_active + limit - 1) // limit if total_active else 1

        return DownloadActivityResponse(
            active=[_activity_entry(item) for item in active],
            recent=[_activity_entry(item) for item in recent],
            page=page,
            limit=limit,
            total_active=total_active,
            total_pages=total_pages,
        )


class LibraryFile(BaseModel):
    """One file on disk belonging to a library item."""

    filename: Annotated[str, Field(description="Original filename from the release")]
    path: Annotated[
        str | None,
        Field(description="Full path inside the mounted filesystem"),
    ]
    file_size: Annotated[int, Field(description="Size in bytes")]
    resolution: Annotated[
        str | None,
        Field(description='Resolution label such as "1080p" or "4K", when known'),
    ]
    codec: Annotated[str | None, Field(description="Video codec, when probed")]
    hdr_type: Annotated[str | None, Field(description="HDR flavour, when probed")]
    available_in_vfs: Annotated[
        bool, Field(description="Whether the file is mounted and playable")
    ]


class LibraryStream(BaseModel):
    """A release found for an item.

    The swarm and size figures are what the indexer claimed at scrape time, not
    a live measurement -- they are reported as `None` when the indexer did not
    say, because "unknown seeders" and "no seeders" mean very different things.
    """

    infohash: Annotated[str, Field(description="Infohash, used to select this release")]
    raw_title: Annotated[str, Field(description="Release title as the indexer gave it")]
    resolution: Annotated[str | None, Field(description="Parsed resolution, when known")]
    rank: Annotated[int, Field(description="RTN rank; higher is preferred")]
    seeders: Annotated[
        int | None, Field(description="Seeders the indexer reported, when it did")
    ]
    leechers: Annotated[
        int | None, Field(description="Leechers the indexer reported, when it did")
    ]
    size: Annotated[
        int | None, Field(description="Size in bytes the indexer reported, when it did")
    ]
    indexer: Annotated[
        str | None, Field(description="Indexer this release came from, when known")
    ]
    is_active: Annotated[
        bool,
        Field(description="Whether this is the release that was actually downloaded"),
    ]
    is_preferred: Annotated[
        bool, Field(description="Whether the user pinned this release")
    ]
    is_blacklisted: Annotated[
        bool, Field(description="Whether this release was rejected and will be skipped")
    ]
    is_downloaded: Annotated[
        bool,
        Field(
            description="Whether this release has a downloaded file, active or kept as an alternate"
        ),
    ]
    is_downloading: Annotated[
        bool,
        Field(description="Whether this release is being fetched in the background"),
    ]


class LibraryStateEntry(BaseModel):
    """Where one TPDB title stands in the local library."""

    riven_id: Annotated[int, Field(description="The Riven media item id")]
    state: Annotated[str, Field(description="The item's current state")]
    title: Annotated[str, Field(description="The item's title in the library")]
    resolution: Annotated[
        str | None,
        Field(description="Best known resolution for the item as a whole"),
    ]
    total_size: Annotated[
        int | None, Field(description="Total bytes on disk, when downloaded")
    ]
    files: Annotated[
        list[LibraryFile],
        Field(default_factory=list, description="Files on disk; empty unless detailed"),
    ]
    streams: Annotated[
        list[LibraryStream],
        Field(
            default_factory=list,
            description="Candidate releases, best first; empty unless detailed",
        ),
    ]


class LibraryStatesResponse(BaseModel):
    states: Annotated[
        dict[str, LibraryStateEntry],
        Field(description="TPDB uuid -> library state, omitting unknown ids"),
    ]


def _library_files(item: MediaItem, mount_path: str) -> list[LibraryFile]:
    """Flatten an item's filesystem entries into view rows.

    Quality is taken from probed metadata where it exists. In practice ffprobe
    often fills in only the frame dimensions, leaving codec and HDR empty, so
    each field is reported independently rather than gated on the whole block
    being present.
    """

    files = list[LibraryFile]()

    for entry in item.filesystem_entries or []:
        metadata = getattr(entry, "media_metadata", None)
        video = metadata.video if metadata else None

        # Path generation walks naming rules and can legitimately fail (an
        # entry whose item was detached, say); a missing path must not cost
        # the caller the size and quality it came for.
        path = None

        try:
            paths = entry.get_all_vfs_paths()

            if paths:
                path = f"{mount_path.rstrip('/')}{paths[0]}"
        except Exception as exc:
            logger.debug(f"Could not resolve VFS path for entry {entry.id}: {exc}")

        files.append(
            LibraryFile(
                filename=getattr(entry, "original_filename", "") or "",
                path=path,
                file_size=entry.file_size or 0,
                resolution=video.resolution_label if video else None,
                codec=video.codec if video else None,
                hdr_type=video.hdr_type if video else None,
                available_in_vfs=bool(entry.available_in_vfs),
            )
        )

    return files


def _library_streams(item: MediaItem) -> list[LibraryStream]:
    """Candidate releases for an item, best rank first.

    Blacklisted releases are included rather than hidden: a user looking at this
    list wants to know a release was tried and rejected, not to wonder why an
    obvious candidate is missing.
    """

    active_hash = item.active_stream.infohash if item.active_stream else None
    preferred_hash = item.preferred_stream_hash
    downloading_hash = item.downloading_stream_hash
    blacklisted = {s.infohash for s in item.blacklisted_streams or []}
    downloaded_hashes = {
        entry.stream_infohash
        for entry in item.filesystem_entries or []
        if getattr(entry, "stream_infohash", None)
    }

    # The two collections are meant to be disjoint, but a stream that appears
    # in both would otherwise be rendered twice.
    by_hash = {
        stream.infohash: stream
        for stream in list(item.streams or []) + list(item.blacklisted_streams or [])
    }

    streams = sorted(
        by_hash.values(), key=lambda stream: stream.rank or 0, reverse=True
    )

    # Truncate by rank, but never let the cap hide a release that was actually
    # tried. A blacklisted release is precisely the one a user comes here to
    # retry, and the pinned or downloaded one has to be visible to be changed.
    kept = streams[:STREAM_LIST_LIMIT]
    kept_hashes = {stream.infohash for stream in kept}
    must_show = (
        blacklisted
        | downloaded_hashes
        | {h for h in (active_hash, preferred_hash, downloading_hash) if h}
    )

    kept.extend(
        stream
        for stream in streams[STREAM_LIST_LIMIT:]
        if stream.infohash in must_show and stream.infohash not in kept_hashes
    )

    return [
        LibraryStream(
            infohash=stream.infohash,
            # RTN writes the literal string "unknown" when it cannot tell,
            # which is noise in a UI -- report nothing instead.
            resolution=(
                stream.resolution
                if stream.resolution and stream.resolution != "unknown"
                else None
            ),
            raw_title=stream.raw_title,
            rank=stream.rank or 0,
            seeders=stream.seeders,
            leechers=stream.leechers,
            size=stream.size,
            indexer=stream.indexer,
            is_active=bool(active_hash and stream.infohash == active_hash),
            is_preferred=bool(preferred_hash and stream.infohash == preferred_hash),
            is_blacklisted=stream.infohash in blacklisted,
            is_downloaded=stream.infohash in downloaded_hashes,
            is_downloading=bool(
                downloading_hash and stream.infohash == downloading_hash
            ),
        )
        for stream in kept
    ]


def _item_resolution(item: MediaItem, files: list[LibraryFile]) -> str | None:
    """Best single resolution to show for an item.

    Probed video wins, because it describes the file that actually landed. The
    release Riven settled on is the fallback -- its title was parsed, not
    measured, but it beats showing nothing. Both are frequently absent for
    adult releases, whose filenames rarely carry a resolution at all.
    """

    for file in files:
        if file.resolution:
            return file.resolution

    active_hash = item.active_stream.infohash if item.active_stream else None

    if active_hash:
        for stream in item.streams or []:
            if stream.infohash == active_hash and stream.resolution:
                return stream.resolution if stream.resolution != "unknown" else None

    return None


@router.get(
    "/library_states",
    summary="Library State by TPDB or Adult Empire ID",
    description="Look up which of the given adult ids already exist in the library",
    operation_id="get_library_states",
    response_model=LibraryStatesResponse,
)
async def get_library_states(
    tpdb_ids: Annotated[
        list[str] | None,
        Query(
            description="TPDB uuids to look up. Ids not in the library are omitted.",
        ),
    ] = None,
    adultempire_ids: Annotated[
        list[str] | None,
        Query(
            description=(
                "Adult Empire product ids to look up. Brochure titles carry no "
                "TPDB id, so this is how they are addressed."
            ),
        ),
    ] = None,
    item_ids: Annotated[
        list[int] | None,
        Query(
            description=(
                "Riven item ids to look up. Needed for a title with no "
                "external id at all -- one TPDB has no confident match for -- "
                "which is addressable only by its own id."
            ),
        ),
    ] = None,
    detailed: Annotated[
        bool,
        Query(
            description="Include per-file details and candidate releases. "
            "A detail page wants these; a poster grid does not."
        ),
    ] = False,
) -> LibraryStatesResponse:
    """Resolve TPDB uuids to library state in one round trip.

    Detail pages and poster grids each need to know whether a title is already
    requested, downloading or available. Asking per title would be a request per
    card, so this takes the whole set at once and simply leaves out the ids it
    does not know -- absence is the answer for those, not an error.
    """

    # Bound each set so a hand-written query string cannot turn into an
    # unbounded IN clause.
    wanted = [tpdb_id for tpdb_id in dict.fromkeys(tpdb_ids or ()) if tpdb_id][:200]
    wanted_ae = [
        ae_id for ae_id in dict.fromkeys(adultempire_ids or ()) if ae_id
    ][:200]
    wanted_ids = [i for i in dict.fromkeys(item_ids or ()) if i][:200]

    if not wanted and not wanted_ae and not wanted_ids:
        return LibraryStatesResponse(states={})

    with db_session() as session:
        conditions = []

        if wanted:
            conditions.append(MediaItem.tpdb_id.in_(wanted))

        if wanted_ae:
            conditions.append(MediaItem.adultempire_id.in_(wanted_ae))

        if wanted_ids:
            conditions.append(MediaItem.id.in_(wanted_ids))

        items = (
            session.execute(select(MediaItem).where(or_(*conditions)))
            .unique()
            .scalars()
            .all()
        )

        states = dict[str, LibraryStateEntry]()

        mount_path = str(settings_manager.settings.filesystem.mount_path)

        for item in items:
            # Keyed by whichever id the caller asked for. A title that has been
            # enriched carries both, so it must answer to the Adult Empire id
            # the brochure knows it by as well as its TPDB one.
            keys = [
                key
                for key in (item.tpdb_id, item.adultempire_id)
                if key and (key in wanted or key in wanted_ae)
            ]

            # Also answer to its own id when asked for that way, which for an
            # item with no external id is the only way it can be asked for.
            if item.id in wanted_ids:
                keys.append(str(item.id))

            if not keys:
                continue

            state = item.last_state or item.state
            files = _library_files(item, mount_path) if detailed else []
            streams = _library_streams(item) if detailed else []

            entries = list(item.filesystem_entries or [])
            total_size = sum(entry.file_size or 0 for entry in entries) or None

            entry = LibraryStateEntry(
                riven_id=item.id,
                state=state.name if state else States.Unknown.name,
                title=item.title,
                resolution=_item_resolution(item, files),
                total_size=total_size,
                files=files,
                streams=streams,
            )

            for key in keys:
                states[key] = entry

    return LibraryStatesResponse(states=states)


@router.get(
    "/{id}",
    summary="Get Media Item by ID",
    description="Fetch a single media item by item ID",
    operation_id="get_item",
)
async def get_item(
    id: Annotated[
        str,
        Path(
            description="""
                The ID of the media item. For 'item' type, use the numeric item ID;
                for 'movie' or 'tv' types, use the TMDB or TVDB ID respectively.
            """,
        ),
    ],
    media_type: Annotated[
        Literal["movie", "tv", "item"],
        Query(description="The type of media item"),
    ],
    extended: Annotated[
        bool,
        Query(description="Whether to include extended information"),
    ] = False,
) -> dict[str, Any]:
    if not id:
        raise HTTPException(status_code=400, detail="No ID or media type provided")

    with db_session() as session:
        match media_type:
            case "movie":
                # needs to be a string
                query = select(MediaItem).where(
                    MediaItem.tmdb_id == id,
                )
            case "tv":
                # needs to be a string
                query = select(MediaItem).where(
                    MediaItem.tvdb_id == id,
                )
            case "item":
                # needs to be an integer
                _id = int(id)
                query = select(MediaItem).where(
                    MediaItem.id == _id,
                )

        try:
            item = session.execute(query).unique().scalar_one_or_none()

            if not item:
                raise HTTPException(status_code=404, detail="Item not found")

            if extended:
                return item.to_extended_dict()

            return item.to_dict()
        except Exception as e:
            # Handle multiple results
            if "Multiple rows were found when one or none was required" in str(e):
                items = session.execute(query).unique().scalars().all()
                duplicate_ids = {item.id for item in items}
                logger.debug(f"Multiple items found with ID {id}: {duplicate_ids}")

                raise HTTPException(
                    status_code=500,
                    detail=f"Multiple items found with ID {id}: {duplicate_ids}",
                )

            logger.error(f"Error fetching item with ID {id}: {str(e)}")

            raise HTTPException(status_code=500, detail=str(e)) from e


_SKIP_RESET_STATES = frozenset({
    States.Completed, States.Unreleased,
    States.Downloaded, States.Symlinked,
    States.Paused,
})


def _reset_scrape_state(item: MediaItem) -> None:
    """Recursively reset scraping state on incomplete children only.

    Walks Show → Season → Episode and clears scraping metadata, streams,
    and failed_attempts on any item not in _SKIP_RESET_STATES, then sets
    its state to Indexed so it re-enters the scraping pipeline.

    Completed / Downloaded / Symlinked / Unreleased / Paused items are
    left untouched.
    """
    if item.last_state in _SKIP_RESET_STATES:
        return

    item.scraped_at = None
    item.scraped_times = 0
    item.failed_attempts = 0
    item.streams.clear()
    item.blacklisted_streams.clear()
    item.active_stream = None
    MediaItem.store_state(item, States.Indexed)

    if isinstance(item, Show):
        for season in item.seasons:
            _reset_scrape_state(season)
    elif isinstance(item, Season):
        for episode in item.episodes:
            _reset_scrape_state(episode)


class ResetResponse(MessageResponse):
    ids: list[int]


@router.post(
    "/reset",
    summary="Reset Media Items",
    description="Reset media items with bases on item IDs",
    operation_id="reset_items",
    response_model=ResetResponse,
)
async def reset_items(
    payload: Annotated[
        IdListPayload,
        Body(description="Reset items payload"),
    ],
) -> ResetResponse:
    """
    Reset the specified media items to their initial state and trigger a media-server library refresh when applicable.

    Parameters:
        request (Request): FastAPI request object used to access application services.
        ids (str): Comma-separated list of item IDs (e.g., "1,2,3") to reset.

    Returns:
        ResetResponse: Dictionary with a human-readable message and the list of processed item IDs:
            - message (str): Summary of the performed reset.
            - ids (list[int]): The numeric IDs that were processed.

    Raises:
        HTTPException: Raised with status 400 when the provided `ids` string cannot be parsed into valid IDs.
    """

    parsed_ids = handle_ids(payload.ids)

    services = di[Program].services

    assert services, "Program services not initialized"

    # Get updater service for media server refresh
    updater = services.updater

    try:
        # Load items using ORM
        with db_session() as session:
            items = (
                session.execute(select(MediaItem).where(MediaItem.id.in_(parsed_ids)))
                .scalars()
                .all()
            )

            for media_item in items:
                try:
                    # Gather all refresh paths before reset (entry may appear at multiple VFS paths)
                    refresh_paths = list[str]()

                    media_entry = media_item.media_entry

                    if updater and media_entry:
                        vfs_paths = media_entry.get_all_vfs_paths()

                        for vfs_path in vfs_paths:
                            abs_path = os.path.join(
                                updater.library_path, vfs_path.lstrip("/")
                            )

                            if isinstance(media_item, Movie):
                                refresh_path = os.path.dirname(
                                    os.path.dirname(abs_path)
                                )
                            else:  # show
                                refresh_path = os.path.dirname(
                                    os.path.dirname(os.path.dirname(abs_path))
                                )
                            if refresh_path not in refresh_paths:
                                refresh_paths.append(refresh_path)

                    def mutation(i: MediaItem, s: Session):
                        """
                        Blacklist the MediaItem's currently active stream and reset the item's state.

                        Parameters:
                            i (MediaItem): The item to mutate.
                            s (Session): Database session (provided for caller context; not used directly here).
                        """

                        i.blacklist_active_stream()
                        if isinstance(i, (Show, Season)):
                            _reset_scrape_state(i)
                        else:
                            i.reset()

                    apply_item_mutation(
                        di[Program],
                        session,
                        media_item,
                        mutation,
                        bubble_parents=True,
                    )

                    session.commit()

                    # Trigger media server refresh for all paths where this item appeared
                    if updater and updater.initialized:
                        for refresh_path in refresh_paths:
                            updater.refresh_path(refresh_path)
                            logger.debug(
                                f"Triggered media server refresh for {refresh_path}"
                            )

                except ValueError as e:
                    logger.error(
                        f"Failed to reset item with id {media_item.id}: {str(e)}"
                    )
                    continue
                except Exception as e:
                    logger.error(
                        f"Unexpected error while resetting item with id {media_item.id}: {str(e)}"
                    )
                    continue
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return ResetResponse(
        message=f"Reset items with id {parsed_ids}",
        ids=parsed_ids,
    )


class RetryResponse(MessageResponse):
    ids: Annotated[
        Sequence[int],
        Field(description="The IDs to retry", min_length=1),
    ]


@router.post(
    "/retry",
    summary="Retry Media Items",
    description="Retry media items with bases on item IDs",
    operation_id="retry_items",
    response_model=RetryResponse,
)
async def retry_items(
    payload: Annotated[
        IdListPayload,
        Body(description="Retry items payload"),
    ],
) -> RetryResponse:
    """Re-add items to the queue"""

    parsed_ids = handle_ids(payload.ids)

    with db_session() as session:
        for id in parsed_ids:
            try:
                item = session.get(MediaItem, id)

                if item:

                    def mutation(i: MediaItem, s: Session):
                        _reset_scrape_state(i)

                    apply_item_mutation(
                        program=di[Program],
                        session=session,
                        item=item,
                        mutation_fn=mutation,
                        bubble_parents=True,
                    )

                    session.commit()

                    di[Program].em.add_event(Event("RetryItem", id))
            except ValueError as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
                )

    return RetryResponse(
        message=f"Retried items with ids {parsed_ids}",
        ids=parsed_ids,
    )


@router.post(
    "/retry_library",
    summary="Retry Library Items",
    description="Retry items in the library that failed to download",
    operation_id="retry_library_items",
    response_model=RetryResponse,
)
async def retry_library_items() -> RetryResponse:
    item_ids = db_functions.retry_library()

    for item_id in item_ids:
        di[Program].em.add_event(
            Event(
                emitted_by="RetryLibrary",
                item_id=item_id,
            )
        )

    return RetryResponse(
        message=f"Retried {len(item_ids)} items",
        ids=item_ids,
    )


class RemoveResponse(BaseModel):
    message: str
    ids: Annotated[
        list[int],
        Field(description="The IDs to remove"),
    ]


@router.delete(
    "/remove",
    summary="Remove Media Items",
    description="Remove media items based on item IDs",
    operation_id="remove_item",
    response_model=RemoveResponse,
)
async def remove_item(
    payload: Annotated[
        IdListPayload,
        Body(description="Remove items payload"),
    ],
) -> RemoveResponse:
    """
    Remove one or more media items identified by their IDs.

    Deletes the MediaItem rows and their related data (joined-table rows, hierarchical children, subtitles, and stream relations) and coordinates related side effects: cancels active jobs for the item, triggers a media server library refresh for the item's library path when an Updater service is available and initialized.

    Parameters:
        request (Request): FastAPI request object (used to access application services).
        ids (str): Comma-separated string of one or more numeric item IDs.

    Returns:
        dict: Response containing a human-readable message and the list of removed item IDs, e.g. {"message": "...", "ids": [1,2]}.

    Raises:
        HTTPException: If no IDs are provided or if an item type is not removable (only "movie" and "show" are allowed).
    """

    parsed_ids = handle_ids(payload.ids)

    if not parsed_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No IDs provided"
        )

    services = di[Program].services

    assert services, "Program services not initialized"

    # Get services
    updater = services.updater
    removed_ids = list[int]()

    with db_session() as session:
        for item_id in parsed_ids:
            # Load item using ORM
            item = session.get(MediaItem, item_id)

            if not item:
                logger.warning(f"Item {item_id} not found, skipping")
                continue

            # Only allow movies and shows to be removed
            if not isinstance(item, (Movie, Show)):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Only movies and shows can be removed. Item {item_id} is a {item.type}",
                )

            logger.debug(f"Removing item with ID {item.id}")

            # 1. Cancel active jobs (EventManager cancels children too)
            di[Program].em.cancel_job(item.id)

            # 2. Gather all refresh paths before deletion (entry may appear at multiple VFS paths)
            refresh_paths = list[str]()

            if updater and item.filesystem_entry:
                if media_entry := item.media_entry:
                    for vfs_path in media_entry.get_all_vfs_paths():
                        # Check if VFS path is already absolute (filesystem path)
                        # VFS paths are normally VFS-relative (e.g., /movies/...) but could be
                        # absolute filesystem paths in some configurations
                        if os.path.isabs(vfs_path) and not vfs_path.startswith(
                            str(updater.library_path)
                        ):
                            # VFS path is absolute but not under library_path - use as-is
                            abs_path = vfs_path
                        elif os.path.isabs(vfs_path) and vfs_path.startswith(
                            str(updater.library_path)
                        ):
                            # VFS path is already an absolute path under library_path - use as-is
                            abs_path = vfs_path
                        else:
                            # VFS path is VFS-relative - join with library_path
                            abs_path = os.path.join(
                                updater.library_path, vfs_path.lstrip("/")
                            )

                        if isinstance(item, Movie):
                            refresh_path = os.path.dirname(os.path.dirname(abs_path))
                        else:  # show
                            refresh_path = os.path.dirname(
                                os.path.dirname(os.path.dirname(abs_path))
                            )
                        if refresh_path not in refresh_paths:
                            refresh_paths.append(refresh_path)

            # 3. Remove from VFS
            if services.filesystem.riven_vfs:
                services.filesystem.riven_vfs.remove(item)

            # 4. Delete from database using ORM
            session.delete(item)
            session.commit()

            removed_ids.append(item_id)

            logger.debug(f"Deleted item {item_id} from database")

            # 5. Trigger media server refresh for all paths where this item appeared
            if updater and updater.initialized:
                for refresh_path in refresh_paths:
                    updater.refresh_path(refresh_path)
                    logger.debug(f"Triggered media server refresh for {refresh_path}")

    logger.info(f"Successfully removed items: {removed_ids}")

    return RemoveResponse(
        message=f"Removed items with ids {removed_ids}",
        ids=removed_ids,
    )


class StreamsResponse(MessageResponse):
    streams: Annotated[
        list[dict[str, Any]],
        Field(description="The list of streams"),
    ]
    blacklisted_streams: Annotated[
        list[dict[str, Any]],
        Field(description="The list of blacklisted streams"),
    ]


@router.get(
    "/{item_id}/streams",
    summary="Get Media Item Streams",
    description="Get streams for a media item",
    operation_id="get_item_streams",
    response_model=StreamsResponse,
)
async def get_item_streams(
    item_id: Annotated[
        int,
        Path(description="The ID of the media item", ge=1),
    ],
) -> StreamsResponse:
    with db_session() as session:
        item = (
            session.execute(select(MediaItem).where(MediaItem.id == item_id))
            .unique()
            .scalar_one_or_none()
        )

    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Item not found"
        )

    downloaded_hashes = {
        entry.stream_infohash
        for entry in item.filesystem_entries
        if getattr(entry, "stream_infohash", None)
    }
    active_hash = item.active_stream.infohash if item.active_stream else None

    def _annotate(stream: Stream) -> dict[str, Any]:
        data = stream.to_dict()
        data["is_active"] = stream.infohash == active_hash
        data["is_downloaded"] = stream.infohash in downloaded_hashes
        data["is_downloading"] = stream.infohash == item.downloading_stream_hash
        return data

    return StreamsResponse(
        message=f"Retrieved streams for item {item_id}",
        streams=[_annotate(stream) for stream in item.streams],
        blacklisted_streams=[stream.to_dict() for stream in item.blacklisted_streams],
    )


def _clear_download(item: MediaItem) -> None:
    """Undo an item's download while keeping its candidate releases.

    Returns the item to `Scraped`, which is what the Downloader picks up.
    `MediaItem.reset()` cannot be used for this: it also clears `streams`, so
    the item would drop back to `Indexed` and need re-scraping.
    """

    from program.program import riven

    if riven.services:
        filesystem_service = riven.services.filesystem

        # Remove VFS nodes before clearing the entries they are derived from.
        if filesystem_service.riven_vfs:
            filesystem_service.riven_vfs.remove(item)

    item.filesystem_entries.clear()
    item.subtitles.clear()
    item.active_stream = None
    item.updated = False
    item.store_state()


@router.post(
    "/{item_id}/streams/{infohash}/select",
    summary="Select a release for download",
    description=(
        "Pin a specific candidate release. The downloader will try it ahead of "
        "its own quality ordering, replacing whatever is currently downloaded."
    ),
    operation_id="select_item_stream",
    response_model=MessageResponse,
)
async def select_stream(
    item_id: Annotated[int, Path(description="The ID of the media item", ge=1)],
    infohash: Annotated[
        str,
        Path(description="Infohash of the release to download", min_length=8),
    ],
) -> MessageResponse:
    """
    Download a particular release instead of the one Riven chose.

    Selecting un-blacklists the release first: a user picking something Riven
    previously rejected is overriding that rejection, and leaving it blacklisted
    would make the request silently do nothing.
    """

    with db_session() as session:
        item = (
            session.execute(select(MediaItem).where(MediaItem.id == item_id))
            .unique()
            .scalar_one_or_none()
        )

        if not item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Item not found"
            )

        candidates = list(item.streams or []) + list(item.blacklisted_streams or [])
        stream = next((s for s in candidates if s.infohash == infohash), None)

        if not stream:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No such release for this item",
            )

        if item.active_stream and item.active_stream.infohash == infohash:
            return MessageResponse(
                message="That release is already the one downloaded",
            )

        # If this stream was already downloaded as a previous active/candidate
        # entry, just switch back to it instead of re-downloading.
        existing_entry = next(
            (
                entry
                for entry in item.filesystem_entries
                if getattr(entry, "stream_infohash", None) == infohash
            ),
            None,
        )

        has_active_download = item.active_stream is not None and item.media_entry is not None
        previously_mounted_entry = item.media_entry if existing_entry is not None else None

        def mutation(i: MediaItem, s: Session):
            i.preferred_stream_hash = infohash

            if stream in (i.blacklisted_streams or []):
                i.unblacklist_stream(stream)

            if existing_entry is not None:
                # Already downloaded: flip which entry is active, no re-fetch.
                for entry in i.filesystem_entries:
                    entry.is_active = entry is existing_entry
                i.active_stream = ActiveStream(
                    infohash=infohash,
                    id=getattr(existing_entry, "provider_download_id", None) or infohash,
                )
                i.downloading_stream_hash = None
            elif has_active_download:
                # Something is already playing: fetch this candidate in the
                # background and switch once it succeeds, keeping the current
                # download untouched in the meantime.
                i.downloading_stream_hash = infohash
            else:
                # Nothing downloaded yet: original behavior, refetch inline.
                _clear_download(i)

        apply_item_mutation(
            di[Program],
            session,
            item,
            mutation,
            bubble_parents=True,
        )

        session.commit()

        if existing_entry is not None:
            from program.program import riven

            riven_vfs = riven.services.filesystem.riven_vfs if riven.services else None

            if riven_vfs and previously_mounted_entry is not None:
                # Unregister the entry that's actually still mounted (not
                # `item.media_entry`, which already reflects the new active
                # flag post-commit) before mounting the newly-active one.
                if previously_mounted_entry is not existing_entry:
                    video_paths = riven_vfs._unregister_filesystem_entry(
                        previously_mounted_entry
                    )
                    previously_mounted_entry.available_in_vfs = False

                    for subtitle in item.subtitles:
                        riven_vfs._unregister_filesystem_entry(
                            subtitle, video_paths=video_paths
                        )
                        subtitle.available_in_vfs = False

                riven_vfs.add(item)
                item.store_state()
                session.commit()

            return MessageResponse(
                message=f"Switched playback to the already-downloaded release {stream.raw_title}",
            )

        di[Program].em.add_event(Event("Downloader", item_id))

        if has_active_download:
            return MessageResponse(
                message=f"Downloading {stream.raw_title} in the background; "
                "playback will switch to it once it's ready",
            )

        return MessageResponse(
            message=f"Selected {stream.raw_title}; it will be downloaded shortly",
        )


@router.post(
    "/{item_id}/streams/{stream_id}/blacklist",
    summary="Blacklist Media Item Stream",
    description="Blacklist a stream for a media item",
    operation_id="blacklist_item_stream",
    response_model=MessageResponse,
)
async def blacklist_stream(
    item_id: Annotated[
        int,
        Path(
            description="The ID of the media item",
            ge=1,
        ),
    ],
    stream_id: Annotated[
        int,
        Path(
            description="The ID of the stream",
            ge=1,
        ),
    ],
) -> MessageResponse:
    with db_session() as session:
        item = (
            session.execute(select(MediaItem).where(MediaItem.id == item_id))
            .unique()
            .scalar_one_or_none()
        )

        if not item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Item not found",
            )

        stream = next(
            (stream for stream in item.streams if stream.id == stream_id), None
        )

        if not stream:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Stream not found",
            )

        def mutation(i: MediaItem, s: Session):
            i.blacklist_stream(stream)

        apply_item_mutation(
            di[Program],
            session,
            item,
            mutation,
            bubble_parents=True,
        )

        session.commit()

        return MessageResponse(
            message=f"Blacklisted stream {stream_id} for item {item_id}",
        )


@router.post(
    "/{item_id}/streams/{stream_id}/unblacklist",
    summary="Unblacklist Media Item Stream",
    description="Unblacklist a stream for a media item",
    operation_id="unblacklist_item_stream",
    response_model=MessageResponse,
)
async def unblacklist_stream(
    item_id: Annotated[
        int,
        Path(
            description="The ID of the media item",
            ge=1,
        ),
    ],
    stream_id: Annotated[
        int,
        Path(
            description="The ID of the stream",
            ge=1,
        ),
    ],
) -> MessageResponse:
    with db_session() as db:
        item = (
            db.execute(select(MediaItem).where(MediaItem.id == item_id))
            .unique()
            .scalar_one_or_none()
        )

        if not item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Item not found"
            )

        stream = next(
            (stream for stream in item.blacklisted_streams if stream.id == stream_id),
            None,
        )

        if not stream:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Stream not found"
            )

        def mutation(i: MediaItem, s: Session):
            i.unblacklist_stream(stream)

        apply_item_mutation(di[Program], db, item, mutation, bubble_parents=True)

        db.commit()

        return MessageResponse(
            message=f"Unblacklisted stream {stream_id} for item {item_id}",
        )


@router.post(
    path="/{item_id}/streams/reset",
    summary="Reset Media Item Streams",
    description="Reset all streams for a media item",
    operation_id="reset_item_streams",
    response_model=MessageResponse,
)
async def reset_item_streams(
    item_id: Annotated[
        int,
        Path(
            description="The ID of the media item",
            ge=1,
        ),
    ],
) -> MessageResponse:
    with db_session() as session:
        item = (
            session.execute(select(MediaItem).where(MediaItem.id == item_id))
            .unique()
            .scalar_one_or_none()
        )

        if not item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Item not found"
            )

        def mutation(i: MediaItem, s: Session):
            i.streams.clear()
            i.blacklisted_streams.clear()
            i.active_stream = None

        apply_item_mutation(
            di[Program],
            session,
            item,
            mutation,
            bubble_parents=True,
        )

        session.commit()

        return MessageResponse(
            message=f"Successfully reset streams for item {item_id}",
        )


class PauseResponse(MessageResponse):
    ids: Annotated[
        list[int],
        Field(description="The IDs to pause", min_length=1),
    ]


@router.post(
    "/pause",
    summary="Pause Media Items",
    description="Pause media items based on item IDs",
    operation_id="pause_items",
    response_model=PauseResponse,
)
async def pause_items(
    payload: Annotated[
        IdListPayload,
        Body(description="Pause items payload"),
    ],
) -> PauseResponse:
    """Pause items and their children from being processed"""

    parsed_ids = handle_ids(payload.ids)

    try:
        with db_session() as session:
            # Load items using ORM
            items = (
                session.execute(select(MediaItem).where(MediaItem.id.in_(parsed_ids)))
                .scalars()
                .all()
            )

            for media_item in items:
                try:
                    item_id, related_ids = db_functions.get_item_ids(
                        session, media_item.id
                    )
                    all_ids = [item_id] + related_ids

                    # Cancel all related jobs
                    for id in all_ids:
                        di[Program].em.cancel_job(id)
                        di[Program].em.remove_id_from_queues(id)

                    if media_item.last_state not in [
                        States.Paused,
                        States.Failed,
                        States.Completed,
                    ]:

                        def mutation(i: MediaItem, s: Session):
                            i.store_state(States.Paused)

                        apply_item_mutation(
                            di[Program],
                            session,
                            media_item,
                            mutation,
                            bubble_parents=False,
                        )
                        session.commit()

                    logger.info("Successfully paused items.")
                except Exception as e:
                    logger.error(f"Failed to pause {media_item.log_string}: {str(e)}")
                    continue
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return PauseResponse(
        message="Successfully paused items.",
        ids=parsed_ids,
    )


@router.post(
    "/unpause",
    summary="Unpause Media Items",
    description="Unpause media items based on item IDs",
    operation_id="unpause_items",
    response_model=PauseResponse,
)
async def unpause_items(
    payload: Annotated[
        IdListPayload,
        Body(description="Unpause items payload"),
    ],
) -> PauseResponse:
    """Unpause items and their children to resume processing"""

    parsed_ids = handle_ids(payload.ids)

    try:
        with db_session() as session:
            # Load items using ORM
            items = (
                session.execute(select(MediaItem).where(MediaItem.id.in_(parsed_ids)))
                .scalars()
                .all()
            )

            for media_item in items:
                try:
                    if media_item.last_state == States.Paused:

                        def mutation(i: MediaItem, s: Session):
                            i.store_state(States.Requested)

                        apply_item_mutation(
                            di[Program],
                            session,
                            media_item,
                            mutation,
                            bubble_parents=True,
                        )

                        session.commit()

                        di[Program].em.add_event(Event("RetryItem", media_item.id))

                        logger.info(f"Successfully unpaused {media_item.log_string}")
                    else:
                        logger.debug(
                            f"Skipping unpause for {media_item.log_string} - not in paused state"
                        )
                except Exception as e:
                    logger.error(f"Failed to unpause {media_item.log_string}: {str(e)}")
                    continue
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return PauseResponse(
        message="Successfully unpaused items.",
        ids=parsed_ids,
    )


def _active_download_ids(states: list[States] | None = None) -> list[int]:
    """Ids of every item currently in the downloads dashboard's `active` set.

    Shared by the queue-wide endpoints below so "pause everything visible" acts
    on the same set the dashboard is showing, computed fresh rather than
    trusting whatever the client last fetched.
    """

    with db_session() as session:
        return list(
            session.execute(
                select(MediaItem.id).where(
                    MediaItem.last_state.in_(states or _ACTIVE_STATES)
                )
            )
            .scalars()
            .all()
        )


@router.post(
    "/downloads/pause_all",
    summary="Pause All In-Flight Downloads",
    description="Pause every item currently in the downloads dashboard's active set",
    operation_id="pause_all_downloads",
    response_model=PauseResponse,
)
async def pause_all_downloads() -> PauseResponse:
    ids = _active_download_ids()

    if not ids:
        return PauseResponse(message="Nothing in flight to pause.", ids=[])

    return await pause_items(IdListPayload(ids=[str(i) for i in ids]))


@router.post(
    "/downloads/resume_all",
    summary="Resume All Paused Downloads",
    description="Resume every item currently paused",
    operation_id="resume_all_downloads",
    response_model=PauseResponse,
)
async def resume_all_downloads() -> PauseResponse:
    ids = _active_download_ids(states=[States.Paused])

    if not ids:
        return PauseResponse(message="Nothing paused to resume.", ids=[])

    return await unpause_items(IdListPayload(ids=[str(i) for i in ids]))


@router.delete(
    "/downloads/cancel_all",
    summary="Cancel All In-Flight Downloads",
    description="Remove every item currently in the downloads dashboard's active set",
    operation_id="cancel_all_downloads",
    response_model=RemoveResponse,
)
async def cancel_all_downloads() -> RemoveResponse:
    ids = _active_download_ids()

    if not ids:
        return RemoveResponse(message="Nothing in flight to cancel.", ids=[])

    return await remove_item(IdListPayload(ids=[str(i) for i in ids]))


class ReindexPayload(BaseModel):
    item_id: Annotated[
        int | None,
        Field(
            default=None,
            description="The ID of the media item",
        ),
    ]
    tvdb_id: Annotated[
        str | None,
        Field(
            default=None,
            description="The TVDB ID of the media item",
        ),
    ]
    tmdb_id: Annotated[
        str | None,
        Field(
            default=None,
            description="The TMDB ID of the media item",
        ),
    ]
    imdb_id: Annotated[
        str | None,
        Field(
            default=None,
            description="The IMDB ID of the media item",
        ),
    ]

    @model_validator(mode="after")
    def check_at_least_one_id_provided(self) -> Self:
        if not any([self.item_id, self.tvdb_id, self.tmdb_id, self.imdb_id]):
            raise ValueError("At least one ID must be provided")

        return self


@router.post(
    path="/reindex",
    summary="Reindex item to pick up new season & episode releases.",
    description="""
        Submits an item to be re-indexed through the indexer to manually fix shows that don't have release dates.
        Only works for movies and shows. Requires item id as a parameter.
    """,
    operation_id="composite_reindexer",
    response_model=MessageResponse,
)
async def reindex_item(
    payload: Annotated[
        ReindexPayload,
        Body(description="Reindex item payload"),
    ],
) -> MessageResponse:
    """Reindex item through Composite Indexer manually"""

    with db_session() as session:
        # Load item using ORM based on provided ID
        item: MediaItem | None = None

        if payload.item_id:
            item = session.get(MediaItem, payload.item_id)
        elif payload.tvdb_id:
            item = session.execute(
                select(MediaItem).where(MediaItem.tvdb_id == payload.tvdb_id)
            ).scalar_one_or_none()
        elif payload.tmdb_id:
            item = session.execute(
                select(MediaItem).where(MediaItem.tmdb_id == payload.tmdb_id)
            ).scalar_one_or_none()
        elif payload.imdb_id:
            item = session.execute(
                select(MediaItem).where(MediaItem.imdb_id == payload.imdb_id)
            ).scalar_one_or_none()

        if not item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Item not found"
            )

        if not isinstance(item, Movie | Show):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Item must be a movie or show",
            )

        try:
            services = di[Program].services

            assert services, "Services not initialized"

            indexer_service = services.indexer

            def mutation(i: MediaItem, s: Session):
                # Reset indexed_at to trigger reindexing
                i.indexed_at = None

                # Run the indexer within the session context
                runner_result = next(indexer_service.run(i, log_msg=True))

                if not runner_result.media_items:
                    raise ValueError(
                        "Failed to reindex item - no data returned from indexer"
                    )

                # Merge the reindexed item back into the session
                # Use no_autoflush to prevent SQLAlchemy from trying to flush
                # the new Season/Episode objects before the merge is complete
                with s.no_autoflush:
                    s.merge(runner_result.media_items[0])

            apply_item_mutation(
                program=di[Program],
                session=session,
                item=item,
                mutation_fn=mutation,
                bubble_parents=True,
            )

            logger.info(f"Successfully re-indexed {item.log_string}")

            di[Program].em.add_event(Event("RetryItem", item.id))

            return MessageResponse(message=f"Successfully re-indexed {item.log_string}")
        except Exception as e:
            logger.error(f"Failed to re-index {item.log_string}: {str(e)}")

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to re-index item: {str(e)}",
            )


class ItemAliasesResponse(BaseModel):
    aliases: Annotated[
        dict[str, list[str]] | None,
        Field(description="The item aliases"),
    ]


@router.get(
    "/{item_id}/aliases",
    summary="Get Media Item Aliases",
    description="Get aliases for a media item",
    operation_id="get_item_aliases",
    response_model=ItemAliasesResponse,
)
async def get_item_aliases(
    item_id: Annotated[
        int,
        Path(
            description="The ID of the media item",
            ge=1,
        ),
    ],
) -> ItemAliasesResponse:
    """Get aliases for a media item"""

    with db_session() as session:
        item = (
            session.execute(select(MediaItem).where(MediaItem.id == item_id))
            .unique()
            .scalar_one_or_none()
        )

    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Item not found"
        )

    return ItemAliasesResponse(aliases=item.aliases)


@router.get(
    "/{item_id}/metadata",
    summary="Get Media Item Metadata",
    description="Get metadata for a media item using item ID",
    operation_id="get_item_metadata",
    response_model=MediaMetadata,
)
async def get_item_metadata(
    item_id: Annotated[
        int,
        Path(
            description="The ID of the media item",
            ge=1,
        ),
    ],
) -> MediaMetadata:
    """Get all metadata for a media item using item ID"""

    with db_session() as session:
        item = (
            session.execute(select(MediaItem).where(MediaItem.id == item_id))
            .unique()
            .scalar_one_or_none()
        )

        if not item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Item not found"
            )

        media_entry = item.media_entry

        if not media_entry or not media_entry.media_metadata:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No metadata available for this item",
            )

        return media_entry.media_metadata


class TpdbAssociationBody(BaseModel):
    #: `None` clears the association, which is a deliberate action rather than
    #: a failure: a title TPDB has no confident record for is better off
    #: showing the metadata it already has than another film's.
    tpdb_id: str | None = None


@router.post(
    "/{item_id}/tpdb",
    summary="Set or clear an item's TPDB association",
    operation_id="set_item_tpdb",
)
async def set_item_tpdb(
    item_id: Annotated[int, Path(description="The ID of the media item", ge=1)],
    body: TpdbAssociationBody,
) -> dict[str, Any]:
    """Point an item at a different TPDB record, or detach it entirely.

    The automatic matcher refuses to guess when the evidence is weak, which is
    correct, but leaves no way to supply an answer a human is sure of -- and
    no way to withdraw one it got wrong. Both directions are needed: a wrong
    association renders a different film's cast, poster and description over
    a title that is otherwise perfectly correct.

    Clearing is safe here specifically because the UI has a riven-id detail
    route to fall back on; before that existed, an item with no external id
    was dropped from the library grid entirely.
    """

    with db_session() as session:
        item = session.execute(
            select(MediaItem).where(MediaItem.id == item_id)
        ).unique().scalar_one_or_none()

        if not item:
            raise HTTPException(status_code=404, detail="Item not found")

        previous = item.tpdb_id
        item.tpdb_id = (body.tpdb_id or "").strip() or None

        # Set on BOTH paths, not just on clearing. Attaching by hand is just
        # as much a decision, and leaving it unlocked would let the matcher
        # overwrite a correct answer with whatever it prefers.
        item.tpdb_locked = True

        cleared_poster = False

        if item.tpdb_id != previous:
            # The poster has to go with the association that supplied it.
            # Detaching a wrong record while keeping its artwork leaves the
            # item showing another film's cover, which is the same bug in a
            # quieter form -- and harder to notice, because the title beside
            # it is now correct.
            #
            # Matched on the TPDB CDN host rather than cleared outright: these
            # items can equally carry a poster from the storefront they were
            # requested from, and that one is still right.
            if item.poster_path and "theporndb.net" in item.poster_path:
                item.poster_path = None
                cleared_poster = True

                # Fall back to the artwork the catalogue entry this item was
                # requested from already carries. It is a poster for THIS
                # title, from the storefront, and still correct -- strictly
                # better than leaving the item with no cover at all.
                fallback = (
                    session.query(CollectionEntry)
                    .filter(
                        CollectionEntry.title == item.title,
                        CollectionEntry.poster_path.isnot(None),
                    )
                    .first()
                )

                if fallback:
                    item.poster_path = fallback.poster_path

            session.commit()
            logger.info(
                f"TPDB association for {item.log_string} changed: "
                f"{previous or 'none'} -> {item.tpdb_id or 'none'}"
                + (" (dropped its TPDB poster)" if cleared_poster else "")
            )

        return {
            "item_id": item_id,
            "tpdb_id": item.tpdb_id,
            "previous": previous,
            "cleared_poster": cleared_poster,
            "locked": True,
        }
