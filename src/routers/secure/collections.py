"""Collection browsing and promotion endpoints.

A collection is a catalogue that sits beside the library. Listing a collection
returns entries, not MediaItems -- most entries have no MediaItem at all, and
that is the normal state. ``requested`` on an entry is what distinguishes "we
know this title exists" from "this title is in your library".

Promotion is one-way and explicit: POST an entry to request it. There is no
endpoint to request a whole collection, because that is exactly the flood the
model exists to prevent; the awards service auto-requests winners on a bounded
schedule instead.
"""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query
from kink import di
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import Integer, func, select

from program.db.db import db_session
from program.media.collection import (
    MATCH_MATCHED,
    Collection,
    CollectionEntry,
)
from program.media.item import MediaItem
from program.services.collections import service as user_collections
from program.settings import settings_manager
from routers.models.shared import MessageResponse

router = APIRouter(prefix="/collections", tags=["collections"])


class CollectionEntryResponse(BaseModel):
    id: int
    title: str
    studio: str | None
    performers: list[str] | None
    category: str | None
    year: int | None
    winner: bool
    tpdb_id: str | None
    tpdb_kind: str | None
    external_source: str | None
    external_id: str | None
    rank: int | None
    rating: float | None
    duration_minutes: int | None
    match_state: str
    poster_path: str | None
    requested: bool
    # Whether this entry can be requested as it stands -- either it resolved to
    # a TPDB title, or its source gave enough metadata to act on directly.
    actionable: bool
    media_item_id: int | None
    state: str | None


class CollectionSummary(BaseModel):
    """A collection without its entries, for the shelf view.

    The counts are what make a collection legible at a glance, and they are
    computed in SQL rather than by loading entries -- a year can hold several
    hundred.
    """

    id: int
    key: str
    source: str
    name: str
    description: str | None
    year: int | None
    poster_path: str | None
    refreshed_at: datetime | None
    total: int
    winners: int
    matched: int
    requested: int


class CollectionDetail(CollectionSummary):
    entries: list[CollectionEntryResponse]


class RequestResponse(BaseModel):
    message: str
    entry_id: int
    media_item_id: int | None


def _entry_response(
    entry: CollectionEntry, states: dict[int, str]
) -> "CollectionEntryResponse":
    """Map one entry to its API shape."""

    return CollectionEntryResponse(
        id=entry.id,
        title=entry.title,
        studio=entry.studio,
        performers=entry.performers,
        category=entry.category,
        year=entry.year,
        winner=entry.winner,
        tpdb_id=entry.tpdb_id,
        tpdb_kind=entry.tpdb_kind,
        external_source=entry.external_source,
        external_id=entry.external_id,
        rank=entry.rank,
        rating=entry.rating,
        duration_minutes=entry.duration_minutes,
        match_state=entry.match_state,
        poster_path=entry.poster_path,
        requested=entry.media_item_id is not None,
        actionable=entry.actionable,
        media_item_id=entry.media_item_id,
        state=states.get(entry.media_item_id) if entry.media_item_id else None,
    )


def _summaries(
    source: str | None = None, key: str | None = None
) -> list[CollectionSummary]:
    """Collections with their counts, computed in one grouped query.

    ``key`` narrows to a single collection so the detail endpoint does not have
    to count every year to render one.
    """

    with db_session() as session:
        query = select(Collection).order_by(Collection.year.desc(), Collection.key)

        if source:
            query = query.where(Collection.source == source)

        if key:
            query = query.where(Collection.key == key)

        collections = session.execute(query).scalars().all()

        if not collections:
            return []

        ids = [c.id for c in collections]

        counts = {
            cid: {"total": 0, "winners": 0, "matched": 0, "requested": 0}
            for cid in ids
        }

        rows = session.execute(
            select(
                CollectionEntry.collection_id,
                func.count(CollectionEntry.id),
                func.sum(func.cast(CollectionEntry.winner, Integer)),
                func.sum(func.cast(CollectionEntry.match_state == MATCH_MATCHED, Integer)),
                func.sum(func.cast(CollectionEntry.media_item_id.is_not(None), Integer)),
            )
            .where(CollectionEntry.collection_id.in_(ids))
            .group_by(CollectionEntry.collection_id)
        ).all()

        for cid, total, winners, matched, requested in rows:
            counts[cid] = {
                "total": total or 0,
                "winners": winners or 0,
                "matched": matched or 0,
                "requested": requested or 0,
            }

        return [
            CollectionSummary(
                id=c.id,
                key=c.key,
                source=c.source,
                name=c.name,
                description=c.description,
                year=c.year,
                poster_path=c.poster_path,
                refreshed_at=c.refreshed_at,
                **counts[c.id],
            )
            for c in collections
        ]


@router.get("", operation_id="list_collections")
def list_collections(
    source: Annotated[str | None, Query(description="Filter by source, e.g. 'avn'")] = None,
) -> list[CollectionSummary]:
    """Every collection, newest year first, with counts but without entries."""

    return _summaries(source)


@router.get("/{key}", operation_id="get_collection")
def get_collection(
    key: Annotated[str, Path(description="Collection key, e.g. 'avn-2026'")],
    winners_only: Annotated[bool, Query()] = False,
    matched_only: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> CollectionDetail:
    """One collection with its entries.

    Winners sort first, then by category, so the page reads as a ballot rather
    than an arbitrary list.
    """

    with db_session() as session:
        collection = session.execute(
            select(Collection).where(Collection.key == key)
        ).scalar_one_or_none()

        if collection is None:
            raise HTTPException(status_code=404, detail=f"No collection {key!r}")

        query = (
            select(CollectionEntry)
            .where(CollectionEntry.collection_id == collection.id)
            # Ranked listings sort by rank; an award ballot has no rank, so
            # nulls fall through to the winner/category/title ordering.
            .order_by(
                CollectionEntry.rank.is_(None),
                CollectionEntry.rank,
                CollectionEntry.winner.desc(),
                CollectionEntry.category,
                CollectionEntry.title,
            )
            .limit(limit)
        )

        if winners_only:
            query = query.where(CollectionEntry.winner.is_(True))

        if matched_only:
            query = query.where(CollectionEntry.match_state == MATCH_MATCHED)

        entries = session.execute(query).scalars().all()

        # The library state of a requested entry, fetched in one query rather
        # than by walking each entry's relationship.
        item_ids = [e.media_item_id for e in entries if e.media_item_id]
        states: dict[int, str] = {}

        if item_ids:
            for item_id, state in session.execute(
                select(MediaItem.id, MediaItem.last_state).where(
                    MediaItem.id.in_(item_ids)
                )
            ).all():
                states[item_id] = state.value if state else "Unknown"

        summary = next(iter(_summaries(key=key)), None)

        if summary is None:
            raise HTTPException(status_code=404, detail=f"No collection {key!r}")

        return CollectionDetail(
            **summary.model_dump(),
            entries=[_entry_response(e, states) for e in entries],
        )


@router.post("/entries/{entry_id}/request", operation_id="request_collection_entry")
def request_entry(
    entry_id: Annotated[int, Path(description="CollectionEntry id")],
) -> RequestResponse:
    """Promote one entry into the library.

    Only a resolved entry can be requested -- without a TPDB id there is nothing
    for the indexer to look up.
    """

    from program.program import Program

    with db_session() as session:
        entry = session.get(CollectionEntry, entry_id)

        if entry is None:
            raise HTTPException(status_code=404, detail="No such entry")

        if entry.media_item_id is not None:
            return RequestResponse(
                message="Already requested",
                entry_id=entry.id,
                media_item_id=entry.media_item_id,
            )

        if not entry.actionable:
            raise HTTPException(
                status_code=409,
                detail=f"{entry.title!r} has not been matched to a TPDB title",
            )

        # Prefer TPDB when the entry has been resolved: it indexes to richer
        # metadata. Otherwise go on the source's own id, which is the whole
        # point of a self-sourced entry -- no TPDB round trip before download.
        if entry.tpdb_id:
            existing = session.execute(
                select(MediaItem).where(MediaItem.tpdb_id == entry.tpdb_id)
            ).scalar_one_or_none()
            payload = {"tpdb_id": entry.tpdb_id}
        else:
            existing = session.execute(
                select(MediaItem).where(
                    MediaItem.adultempire_id == entry.external_id
                )
            ).scalar_one_or_none()
            payload = {"adultempire_id": entry.external_id}

        if existing is not None:
            entry.media_item_id = existing.id
            session.commit()

            return RequestResponse(
                message="Already in library",
                entry_id=entry.id,
                media_item_id=existing.id,
            )

        item = MediaItem(
            {
                **payload,
                "requested_by": "collections",
                "requested_at": datetime.now(),
            }
        )

        if not di[Program].em.add_item(item):
            logger.debug(f"{entry.title!r} was not queued (already present or running)")

            return RequestResponse(
                message="Not queued (already present or running)",
                entry_id=entry.id,
                media_item_id=None,
            )

        session.commit()

    return RequestResponse(message="Requested", entry_id=entry_id, media_item_id=None)


class BrochureShelf(BaseModel):
    """One row of the brochure: a listing plus the titles to show in it."""

    key: str
    name: str
    description: str | None
    refreshed_at: datetime | None
    total: int
    entries: list[CollectionEntryResponse]


@router.get("/brochure/shelves", operation_id="get_brochure")
def brochure(
    per_shelf: Annotated[int, Query(ge=1, le=100)] = 24,
    source: Annotated[str, Query()] = "adultempire",
) -> list[BrochureShelf]:
    """Every shelf with its top entries, in one request.

    The brochure is a browsing surface, so it is served whole rather than a
    request per row: four shelves would otherwise be four round trips before
    the page paints. Entries come back in rank order and are capped per shelf,
    since a shelf shows a row, not a catalogue.
    """

    with db_session() as session:
        collections = (
            session.execute(
                select(Collection)
                .where(Collection.source == source)
                .order_by(Collection.id)
            )
            .scalars()
            .all()
        )

        if not collections:
            return []

        shelves: list[BrochureShelf] = []

        for collection in collections:
            total = session.scalar(
                select(func.count(CollectionEntry.id)).where(
                    CollectionEntry.collection_id == collection.id
                )
            )

            entries = (
                session.execute(
                    select(CollectionEntry)
                    .where(CollectionEntry.collection_id == collection.id)
                    .order_by(
                        CollectionEntry.rank.is_(None),
                        CollectionEntry.rank,
                        CollectionEntry.title,
                    )
                    .limit(per_shelf)
                )
                .scalars()
                .all()
            )

            item_ids = [e.media_item_id for e in entries if e.media_item_id]
            states: dict[int, str] = {}

            if item_ids:
                for item_id, state in session.execute(
                    select(MediaItem.id, MediaItem.last_state).where(
                        MediaItem.id.in_(item_ids)
                    )
                ).all():
                    states[item_id] = state.value if state else "Unknown"

            shelves.append(
                BrochureShelf(
                    key=collection.key,
                    name=collection.name,
                    description=collection.description,
                    refreshed_at=collection.refreshed_at,
                    total=total or 0,
                    entries=[_entry_response(e, states) for e in entries],
                )
            )

        return shelves


@router.get("/entries/{entry_id}", operation_id="get_collection_entry")
def get_entry(entry_id: Annotated[int, Path()]) -> CollectionEntryResponse:
    """One entry, for a brochure detail page."""

    with db_session() as session:
        entry = session.get(CollectionEntry, entry_id)

        if entry is None:
            raise HTTPException(status_code=404, detail="No such entry")

        states: dict[int, str] = {}

        if entry.media_item_id:
            row = session.execute(
                select(MediaItem.id, MediaItem.last_state).where(
                    MediaItem.id == entry.media_item_id
                )
            ).first()

            if row:
                states[row[0]] = row[1].value if row[1] else "Unknown"

        return _entry_response(entry, states)


@router.get("/{key}/categories", operation_id="list_collection_categories")
def list_categories(
    key: Annotated[str, Path()],
) -> dict[str, int]:
    """Entry counts per award category, for filtering a large collection."""

    with db_session() as session:
        collection = session.execute(
            select(Collection).where(Collection.key == key)
        ).scalar_one_or_none()

        if collection is None:
            raise HTTPException(status_code=404, detail=f"No collection {key!r}")

        rows = session.execute(
            select(CollectionEntry.category, func.count(CollectionEntry.id))
            .where(CollectionEntry.collection_id == collection.id)
            .group_by(CollectionEntry.category)
            .order_by(func.count(CollectionEntry.id).desc())
        ).all()

    return {category or "Uncategorised": count for category, count in rows}


# --------------------------------------------------------------------- AVN

class AvnYear(BaseModel):
    """One ceremony year as a row on the AVN page.

    ``key`` is null for a year whose collection has not been built yet. That is
    the normal state right after enabling: the corpus sync writes years one at
    a time, so the page shows every expected year immediately and fills them in
    as they arrive, rather than appearing empty and then jumping.
    """

    year: int
    key: str | None
    name: str
    # "ready" once the year has entries; "fetching" while it is still being
    # built or resolved.
    status: Literal["ready", "fetching"]
    total: int
    matched: int
    requested: int
    entries: list[CollectionEntryResponse]


class AvnOverview(BaseModel):
    enabled: bool
    years: list[AvnYear]
    # Counts per match state across every year, so the page can say how much of
    # the corpus has been resolved without counting client-side.
    progress: dict[str, int]


@router.get("/avn/overview", operation_id="get_avn_overview")
def avn_overview(
    per_year: Annotated[int, Query(ge=1, le=100)] = 18,
) -> AvnOverview:
    """Every AVN ceremony year, newest first, with a row of winners each.

    Years are enumerated from the settings rather than from the database, so a
    year that has not been fetched yet still gets a row. Without that the page
    would grow downwards as the sync progressed, which reads as breakage rather
    than as progress.
    """

    from program.services.awards.service import SOURCE as AVN_SOURCE

    settings = settings_manager.settings.content.awards
    latest = datetime.now().year
    expected = list(range(latest, settings.first_year - 1, -1))

    with db_session() as session:
        collections = {
            collection.year: collection
            for collection in session.execute(
                select(Collection).where(Collection.source == AVN_SOURCE)
            )
            .scalars()
            .all()
            if collection.year is not None
        }

        # A collection may exist for a year outside the configured range (the
        # setting was lowered after a sync); showing it is strictly better than
        # hiding data that is already there.
        for year in sorted(collections, reverse=True):
            if year not in expected:
                expected.append(year)

        expected.sort(reverse=True)

        rows: list[AvnYear] = []

        for year in expected:
            collection = collections.get(year)

            if collection is None:
                rows.append(
                    AvnYear(
                        year=year,
                        key=None,
                        name=f"AVN Awards {year}",
                        status="fetching",
                        total=0,
                        matched=0,
                        requested=0,
                        entries=[],
                    )
                )
                continue

            total, matched, requested = session.execute(
                select(
                    func.count(CollectionEntry.id),
                    func.sum(
                        func.cast(CollectionEntry.match_state == MATCH_MATCHED, Integer)
                    ),
                    func.sum(
                        func.cast(CollectionEntry.media_item_id.is_not(None), Integer)
                    ),
                ).where(CollectionEntry.collection_id == collection.id)
            ).one()

            entries = (
                session.execute(
                    select(CollectionEntry)
                    .where(CollectionEntry.collection_id == collection.id)
                    # Resolved entries first: they are the ones with artwork, so
                    # a row of them looks like a shelf rather than a list of
                    # placeholder tiles.
                    .order_by(
                        CollectionEntry.poster_path.is_(None),
                        CollectionEntry.winner.desc(),
                        CollectionEntry.category,
                        CollectionEntry.title,
                    )
                    .limit(per_year)
                )
                .scalars()
                .all()
            )

            item_ids = [e.media_item_id for e in entries if e.media_item_id]
            states: dict[int, str] = {}

            if item_ids:
                for item_id, state in session.execute(
                    select(MediaItem.id, MediaItem.last_state).where(
                        MediaItem.id.in_(item_ids)
                    )
                ).all():
                    states[item_id] = state.value if state else "Unknown"

            rows.append(
                AvnYear(
                    year=year,
                    key=collection.key,
                    name=collection.name,
                    status="ready" if total else "fetching",
                    total=total or 0,
                    matched=matched or 0,
                    requested=requested or 0,
                    entries=[_entry_response(e, states) for e in entries],
                )
            )

        progress = {
            state: count
            for state, count in session.execute(
                select(CollectionEntry.match_state, func.count(CollectionEntry.id))
                .join(Collection)
                .where(Collection.source == AVN_SOURCE)
                .group_by(CollectionEntry.match_state)
            ).all()
        }

    return AvnOverview(enabled=settings.enabled, years=rows, progress=progress)


class AvnEnableResponse(BaseModel):
    enabled: bool
    message: str


@router.post("/avn/enable", operation_id="enable_avn")
def enable_avn(enabled: Annotated[bool, Query()] = True) -> AvnEnableResponse:
    """Turn the AVN corpus job on (or off) and apply it without a restart.

    Both halves matter and neither is enough alone: the settings write is what
    survives a restart and what the settings page shows, and the scheduler
    refresh is what makes data start appearing now. Writing the setting without
    refreshing leaves a switch that reads "on" while nothing runs until the
    next restart -- which is exactly the kind of silent no-op this page exists
    to avoid.
    """

    if enabled and not settings_manager.settings.tpdb.api_token:
        raise HTTPException(
            status_code=409,
            detail="Set a ThePornDB API token in Settings first; AVN titles are resolved against TPDB.",
        )

    settings_manager.settings.content.awards.enabled = enabled
    settings_manager.save()

    from program.program import Program

    try:
        di[Program].scheduler_manager.refresh_content_jobs()
    except Exception as exc:
        # The setting is saved either way; say so rather than reporting a
        # failure that a restart would resolve.
        logger.error(f"Could not refresh scheduled jobs: {exc}")

        return AvnEnableResponse(
            enabled=enabled,
            message="Saved, but the scheduler did not pick it up. Restart Riven to apply.",
        )

    return AvnEnableResponse(
        enabled=enabled,
        message=(
            "Fetching AVN award winners. Years will fill in as they resolve."
            if enabled
            else "AVN award collections disabled."
        ),
    )


# ---------------------------------------------------------- user collections

class CreateCollectionRequest(BaseModel):
    name: str
    description: str | None = None


class AddToCollectionRequest(BaseModel):
    """What to add. Exactly one of these identifies a title.

    Three routes in because three surfaces need it: a TPDB detail page knows a
    uuid, a library page knows a Riven id, and a brochure or award page knows a
    catalogue entry id.
    """

    tpdb_id: str | None = None
    tpdb_kind: str = "movie"
    media_item_id: int | None = None
    entry_id: int | None = None


def _user_collection(session, key: str) -> Collection:
    collection = session.execute(
        select(Collection).where(Collection.key == key)
    ).scalar_one_or_none()

    if collection is None:
        raise HTTPException(status_code=404, detail=f"No collection {key!r}")

    if collection.source != user_collections.SOURCE:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{collection.name!r} is built from {collection.source!r} and is "
                "maintained automatically; it cannot be edited by hand."
            ),
        )

    return collection


@router.post("", operation_id="create_collection", status_code=201)
def create_collection(body: CreateCollectionRequest) -> CollectionSummary:
    """Create an empty user collection."""

    with db_session() as session:
        try:
            collection = user_collections.create(
                session, body.name, body.description
            )
        except user_collections.CollectionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        key = collection.key
        session.commit()

    summary = next(iter(_summaries(key=key)), None)

    if summary is None:  # pragma: no cover - the row was just committed
        raise HTTPException(status_code=500, detail="Collection vanished after create")

    return summary


@router.delete("/{key}", operation_id="delete_collection")
def delete_collection(key: Annotated[str, Path()]) -> MessageResponse:
    """Delete a user collection and its entries.

    Entries are catalogue rows, so nothing leaves the library: a title that was
    requested from this collection stays exactly where it is.
    """

    with db_session() as session:
        collection = _user_collection(session, key)
        name = collection.name
        session.delete(collection)
        session.commit()

    return MessageResponse(message=f"Deleted {name!r}")


@router.post("/{key}/items", operation_id="add_to_collection")
def add_to_collection(
    key: Annotated[str, Path(description="Collection key")],
    body: AddToCollectionRequest,
) -> CollectionEntryResponse:
    """Add one title to a user collection.

    Adding does **not** request the title. A collection is a list of titles you
    are interested in; the library is what you own. If the title happens to be
    in the library already the entry adopts it, but nothing is ever queued for
    download from here.
    """

    with db_session() as session:
        collection = _user_collection(session, key)

        try:
            if body.entry_id is not None:
                source_entry = session.get(CollectionEntry, body.entry_id)

                if source_entry is None:
                    raise HTTPException(status_code=404, detail="No such entry")

                entry = user_collections.add_catalogue_entry(
                    session, collection, source_entry
                )
            elif body.media_item_id is not None:
                entry = user_collections.add_library_item(
                    session, collection, body.media_item_id
                )
            elif body.tpdb_id:
                entry = user_collections.add_tpdb_title(
                    session, collection, body.tpdb_id, body.tpdb_kind
                )
            else:
                raise HTTPException(
                    status_code=400,
                    detail="Provide one of tpdb_id, media_item_id or entry_id",
                )
        except user_collections.CollectionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        session.commit()

        states: dict[int, str] = {}

        if entry.media_item_id:
            row = session.execute(
                select(MediaItem.id, MediaItem.last_state).where(
                    MediaItem.id == entry.media_item_id
                )
            ).first()

            if row:
                states[row[0]] = row[1].value if row[1] else "Unknown"

        return _entry_response(entry, states)


@router.delete("/{key}/items/{entry_id}", operation_id="remove_from_collection")
def remove_from_collection(
    key: Annotated[str, Path()],
    entry_id: Annotated[int, Path()],
) -> MessageResponse:
    """Remove one title from a user collection.

    Local only. TPDB's collection route has no DELETE, so a title mirrored
    there stays there and has to be removed on the TPDB website.
    """

    with db_session() as session:
        collection = _user_collection(session, key)
        entry = session.get(CollectionEntry, entry_id)

        if entry is None or entry.collection_id != collection.id:
            raise HTTPException(status_code=404, detail="No such entry in this collection")

        title = entry.title
        session.delete(entry)
        session.commit()

    return MessageResponse(message=f"Removed {title!r}")
