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
    match_state: str
    poster_path: str | None
    requested: bool
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
            .order_by(
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
            entries=[
                CollectionEntryResponse(
                    id=e.id,
                    title=e.title,
                    studio=e.studio,
                    performers=e.performers,
                    category=e.category,
                    year=e.year,
                    winner=e.winner,
                    tpdb_id=e.tpdb_id,
                    tpdb_kind=e.tpdb_kind,
                    match_state=e.match_state,
                    poster_path=e.poster_path,
                    requested=e.media_item_id is not None,
                    media_item_id=e.media_item_id,
                    state=states.get(e.media_item_id) if e.media_item_id else None,
                )
                for e in entries
            ],
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

        if not entry.tpdb_id:
            raise HTTPException(
                status_code=409,
                detail=f"{entry.title!r} has not been matched to a TPDB title",
            )

        existing = session.execute(
            select(MediaItem).where(MediaItem.tpdb_id == entry.tpdb_id)
        ).scalar_one_or_none()

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
                "tpdb_id": entry.tpdb_id,
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
