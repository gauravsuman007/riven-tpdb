"""The Adult Empire studio directory.

Two different kinds of data behind one prefix, and the difference is the whole
design:

    * Studios themselves are mirrored locally and served from the database.
      There are about a hundred, they change about never, and obtaining them
      costs several minutes of crawling.
    * A studio's *titles* are read live from the storefront on every request.
      They are a ranked view, and a rank stored last Sunday is not the rank.

Clicking a title crosses from the second world into the first: the row becomes
a real :class:`CollectionEntry`, at which point it is an ordinary brochure
title and every existing path -- the detail page, requesting, scraping, TPDB
resolution -- applies to it unchanged. That promotion is deliberately per
click. Minting an entry for all forty-eight rows of every studio anyone
glanced at is how a catalogue turns into a library by accident.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import select

from program.db.db import db_session
from program.media.collection import (
    MATCH_SELF_SOURCED,
    Collection,
    CollectionEntry,
)
from program.media.studio import Studio
from program.services.recommendations.adultempire import (
    AdultEmpireError,
    RankedTitle,
)
from program.services.indexers.adultempire_indexer import parse_released
from program.services.recommendations.studios import StudioService
from program.services.recommendations.tpdb_lookup import enrich_entry
from routers.models.shared import MessageResponse

router = APIRouter(prefix="/studios", tags=["studios"])

# Titles promoted from a studio page land here rather than in a per-studio
# collection. They are not a ranked listing and never get rebuilt by a sync --
# they are just the rows someone opened -- so one collection holds them all,
# and a title that is also on a brochure shelf reuses that entry instead.
PROMOTED_KEY = "adultempire-studio-titles"

# One service, and therefore one Adult Empire client, for the whole process.
# The client paces itself to one request a second, and that pacing lives on the
# instance -- building a fresh one per request would reset it every time and
# turn a polite crawler into a burst of concurrent hits on someone else's shop.
_service: "StudioService | None" = None


def service() -> StudioService:
    """The shared studio service, or a 503 if studios are switched off."""

    global _service

    if _service is None or not _service.initialized:
        _service = StudioService()

    if not _service.initialized:
        raise HTTPException(
            status_code=503,
            detail="The Adult Empire studio directory is not enabled",
        )

    return _service


class StudioResponse(BaseModel):
    id: int
    ae_id: str
    name: str
    slug: str | None
    title_count: int | None
    description: str | None
    logo_path: str | None
    poster_path: str | None
    tpdb_site_id: str | None
    saved: bool


class StudioTitle(BaseModel):
    """One row of a studio's ranked listing, before it has an entry."""

    rank: int
    product_id: str
    title: str
    poster: str | None


class StudioRow(BaseModel):
    key: str
    name: str
    description: str
    titles: list[StudioTitle]


class StudioRows(BaseModel):
    """A studio's ranked rows, on their own.

    Split from the studio itself because the two have wildly different costs.
    The studio is a database read; the rows are two live page reads of
    someone else's shop, serialised behind a one-request-a-second courtesy
    delay, so they take several seconds and nothing can make them faster.
    Serving them together meant the page could not paint until the storefront
    answered -- a blank screen for the whole wait, to show a name and a logo
    that were ready immediately.
    """

    rows: list[StudioRow]


# Presentation, not protocol, so it lives here rather than in the client --
# the same split as the brochure's SHELVES.
ROWS: list[tuple[str, str, str]] = [
    ("bestseller", "Top Sellers", "This studio's best-selling titles."),
    ("trending", "Trending Now", "What is moving for this studio right now."),
]


def _response(studio: Studio) -> StudioResponse:
    return StudioResponse(
        id=studio.id,
        ae_id=studio.ae_id,
        name=studio.name,
        slug=studio.slug,
        title_count=studio.title_count,
        description=studio.description,
        logo_path=studio.logo_path,
        poster_path=studio.poster_path,
        tpdb_site_id=studio.tpdb_site_id,
        saved=studio.saved,
    )


@router.get("", operation_id="get_studios")
def list_studios(
    saved: Annotated[bool | None, Query()] = None,
    search: Annotated[str | None, Query()] = None,
    # The ceiling has to clear the whole directory in one request: the
    # picker filters in the browser as the user types, and a cap below
    # the catalogue size would silently hide studios from the search
    # rather than fail visibly. ~1,200 studios today.
    limit: Annotated[int, Query(ge=1, le=5000)] = 200,
) -> list[StudioResponse]:
    """The studio directory.

    ``saved=true`` is what the brochure's studios section asks for; the
    unfiltered list is the picker used to add to it.
    """

    with db_session() as session:
        query = select(Studio)

        if saved is not None:
            query = query.where(Studio.saved.is_(saved))

        if search:
            query = query.where(Studio.name.ilike(f"%{search}%"))

        studios = (
            session.execute(
                # Biggest catalogues first: the directory is a hundred names
                # with no other ordering that means anything to a reader.
                query.order_by(Studio.title_count.desc().nullslast()).limit(limit)
            )
            .scalars()
            .all()
        )

        return [_response(studio) for studio in studios]


@router.post("/{studio_id}/save", operation_id="save_studio")
def save_studio(studio_id: Annotated[int, Path()]) -> StudioResponse:
    """Add a studio to the brochure's studios section."""

    return _set_saved(studio_id, True)


@router.delete("/{studio_id}/save", operation_id="unsave_studio")
def unsave_studio(studio_id: Annotated[int, Path()]) -> StudioResponse:
    """Remove a studio from the brochure's studios section."""

    return _set_saved(studio_id, False)


def _set_saved(studio_id: int, saved: bool) -> StudioResponse:
    with db_session() as session:
        studio = session.get(Studio, studio_id)

        if studio is None:
            raise HTTPException(status_code=404, detail="No such studio")

        studio.saved = saved
        studio.saved_at = datetime.now() if saved else None
        session.commit()

        return _response(studio)


@router.get("/{studio_id}", operation_id="get_studio")
def studio_detail(studio_id: Annotated[int, Path()]) -> StudioResponse:
    """One studio. A database read, so it answers immediately.

    Deliberately does not include the ranked rows -- see :class:`StudioRows`.
    """

    with db_session() as session:
        studio = session.get(Studio, studio_id)

        if studio is None:
            raise HTTPException(status_code=404, detail="No such studio")

        return _response(studio)


@router.get("/{studio_id}/rows", operation_id="get_studio_rows")
def studio_rows(
    studio_id: Annotated[int, Path()],
    per_row: Annotated[int, Query(ge=1, le=48)] = 12,
) -> StudioRows:
    """A studio's ranked rows, read live from the storefront.

    Slow by nature and fetched separately so the page can paint without it.

    A row that fails comes back empty rather than failing the request: these
    are two independent page reads of someone else's shop, and one being
    unavailable should not blank the studio.
    """

    with db_session() as session:
        studio = session.get(Studio, studio_id)

        if studio is None:
            raise HTTPException(status_code=404, detail="No such studio")

        studios = service()

        rows: list[StudioRow] = []

        for sort, name, description in ROWS:
            try:
                titles = studios.listing(studio, sort)
            except AdultEmpireError as exc:
                logger.warning(f"Studio {studio.name} {sort} row failed: {exc}")
                titles = []

            rows.append(
                StudioRow(
                    key=sort,
                    name=name,
                    description=description,
                    titles=[
                        StudioTitle(
                            rank=title.rank,
                            product_id=title.product_id,
                            title=title.title,
                            poster=title.poster,
                        )
                        for title in titles[:per_row]
                    ],
                )
            )

        return StudioRows(rows=rows)


class PromoteResponse(BaseModel):
    entry_id: int
    message: str


@router.post("/titles/{product_id}", operation_id="promote_studio_title")
def promote_title(product_id: Annotated[str, Path()]) -> PromoteResponse:
    """Turn a studio listing row into a brochure entry and return its id.

    The frontend navigates to the ordinary brochure detail page with this id,
    so a studio title and a bestseller title are the same page and the same
    code from here on.

    Idempotent, and it looks across every Adult Empire collection before
    creating anything: a title on a studio page is frequently also on a
    brochure shelf, and two entries for one storefront id would mean two
    detail pages that disagree about whether it had been requested.
    """

    with db_session() as session:
        existing = session.execute(
            select(CollectionEntry)
            .join(Collection)
            .where(
                Collection.source == "adultempire",
                CollectionEntry.external_id == product_id,
            )
            .order_by(CollectionEntry.id)
        ).scalars().first()

        if existing is not None:
            return PromoteResponse(entry_id=existing.id, message="Already known")

        collection = session.execute(
            select(Collection).where(Collection.key == PROMOTED_KEY)
        ).scalar_one_or_none()

        if collection is None:
            collection = Collection(
                key=PROMOTED_KEY,
                source="adultempire",
                name="Studio Titles",
                description="Titles opened from a studio page.",
            )
            session.add(collection)
            session.flush()

        studios = service()

        # The bare "/{id}/" form: a wrong slug still answers 200 but serves a
        # page with none of the product markup, so this would silently find
        # nothing at all.
        probe = RankedTitle(
            product_id=product_id, title="", rank=0, listing="studio",
            url=f"/{product_id}/",
        )

        try:
            detail = studios.client.enrich(probe)
        except AdultEmpireError as exc:
            raise HTTPException(
                status_code=502, detail=f"Adult Empire is unavailable: {exc}"
            ) from exc

        entry = CollectionEntry(
            collection_id=collection.id,
            external_source="adultempire",
            external_id=product_id,
            title=detail.title or f"Adult Empire {product_id}",
            studio=detail.studio,
            year=detail.year,
            rating=detail.rating,
            duration_minutes=detail.duration_minutes,
            released_at=parse_released(detail.released),
            performers=detail.performers or None,
            poster_path=detail.poster,
            category="studio",
            match_state=MATCH_SELF_SOURCED,
        )
        session.add(entry)
        session.flush()

        # Resolved here rather than left to the batch job, because the user is
        # about to be redirected to this entry's detail page and that page
        # picks its view from tpdb_id. Waiting for the timer would show the
        # storefront view once and the real one on a later visit.
        enrich_entry(entry)
        session.commit()

        return PromoteResponse(entry_id=entry.id, message="Added")


@router.post("/sync", operation_id="sync_studios")
def sync_studios() -> MessageResponse:
    """Refresh the directory now, rather than waiting for the weekly run."""

    return MessageResponse(message=f"Synced {service().sync()} studios")
