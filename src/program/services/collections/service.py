"""User-created collections.

A *user* collection is the same :class:`Collection` row as an award ballot or a
storefront listing, distinguished only by ``source == "user"``. Reusing the
model rather than adding a parallel one is deliberate: an entry in a user
collection needs exactly what a catalogue entry needs -- a title, whatever
metadata the source gave, an optional TPDB id, and a ``media_item_id`` that is
null until the title is actually in the library.

That last part is the important one. Adding a title to a collection does **not**
request it. A collection is a list of titles you are interested in; the library
is the list of titles you own. Conflating the two is what the model exists to
avoid, and it is why the add endpoints never touch the event manager.

Three things can be added, and they differ in where the metadata comes from:

    * a TPDB title, by uuid -- metadata fetched from TPDB;
    * a library item, by Riven id -- metadata already local;
    * a catalogue entry, by ``CollectionEntry`` id -- metadata copied across,
      and for an Adult Empire entry a TPDB lookup is attempted so the title
      lands in the collection with the same artwork and ids a TPDB title has.
"""

import re
from datetime import datetime

from program.utils.time import utcnow

from kink import di
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from program.media.collection import (
    MATCH_MATCHED,
    MATCH_SELF_SOURCED,
    MATCH_UNMATCHED,
    Collection,
    CollectionEntry,
)
from program.media.item import MediaItem
from program.services.recommendations.tpdb_lookup import client, enrich_entry
from program.settings import settings_manager

SOURCE = "user"

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


class CollectionError(Exception):
    """A collection operation that failed for a reason worth showing the user."""


def slugify(name: str) -> str:
    """A key fragment for a collection name.

    Names are free text and keys are URL path segments, so the two cannot be
    the same string. An empty result falls back to a timestamp rather than
    producing the key ``user-``, which would collide with every other
    unnameable collection.
    """

    slug = _SLUG_STRIP.sub("-", name.strip().lower()).strip("-")

    return slug or utcnow().strftime("%Y%m%d%H%M%S")


def unique_key(session: Session, name: str) -> str:
    """``user-<slug>``, suffixed until it is free.

    Two collections may legitimately be called the same thing; the key is an
    address, not an identity the user chose.
    """

    base = f"{SOURCE}-{slugify(name)}"
    key = base
    suffix = 2

    while session.execute(
        select(Collection.id).where(Collection.key == key)
    ).scalar_one_or_none() is not None:
        key = f"{base}-{suffix}"
        suffix += 1

    return key


def create(session: Session, name: str, description: str | None = None) -> Collection:
    """Create an empty user collection."""

    name = (name or "").strip()

    if not name:
        raise CollectionError("A collection needs a name")

    collection = Collection(
        key=unique_key(session, name),
        source=SOURCE,
        name=name,
        description=(description or "").strip() or None,
        year=None,
    )

    session.add(collection)
    session.flush()

    logger.debug(f"Created collection {collection.key!r}")

    return collection


def _existing_entry(
    session: Session,
    collection: Collection,
    *,
    tpdb_id: str | None,
    external_id: str | None,
    media_item_id: int | None,
) -> CollectionEntry | None:
    """The entry already representing this title, if there is one.

    The table's unique constraint is (collection, title, category), which does
    not help here: user entries have a null category, and NULL never equals
    NULL in SQL, so the database would happily accept the same title twice.
    Identity for a user entry is whichever id it was added by.
    """

    query = select(CollectionEntry).where(
        CollectionEntry.collection_id == collection.id
    )

    if tpdb_id:
        return session.execute(
            query.where(CollectionEntry.tpdb_id == tpdb_id)
        ).scalars().first()

    if external_id:
        return session.execute(
            query.where(CollectionEntry.external_id == external_id)
        ).scalars().first()

    if media_item_id:
        return session.execute(
            query.where(CollectionEntry.media_item_id == media_item_id)
        ).scalars().first()

    return None


def _link_library(session: Session, entry: CollectionEntry) -> None:
    """Point the entry at a matching library item, if one already exists.

    Only ever *adopts* an existing item. Nothing here creates a MediaItem: an
    entry with a null ``media_item_id`` is a title you are interested in, and
    turning that into a download because it was added to a list is not what the
    user asked for.
    """

    if entry.media_item_id is not None:
        return

    item = None

    if entry.tpdb_id:
        item = session.execute(
            select(MediaItem).where(MediaItem.tpdb_id == entry.tpdb_id)
        ).scalars().first()

    if item is None and entry.external_id:
        item = session.execute(
            select(MediaItem).where(MediaItem.adultempire_id == entry.external_id)
        ).scalars().first()

    if item is not None:
        entry.media_item_id = item.id


def add_tpdb_title(
    session: Session, collection: Collection, tpdb_id: str, kind: str = "movie"
) -> CollectionEntry:
    """Add a TPDB title by uuid, fetching its metadata."""

    existing = _existing_entry(
        session, collection, tpdb_id=tpdb_id, external_id=None, media_item_id=None
    )

    if existing is not None:
        return existing

    title = tpdb_id
    studio = performers = poster = None
    year = None

    api = client()

    if api is not None:
        try:
            detail = (
                api.get_movie(tpdb_id) if kind != "scene" else api.get_scene(tpdb_id)
            )
        except Exception as exc:
            logger.debug(f"TPDB detail lookup failed for {tpdb_id}: {exc}")
            detail = None

        if detail is not None:
            title = detail.title or tpdb_id
            studio = detail.site.name if detail.site else None
            performers = [p.name for p in detail.performers if p.name] or None
            poster = detail.poster or (
                detail.posters.large if detail.posters else None
            )

            if detail.date and len(detail.date) >= 4 and detail.date[:4].isdigit():
                year = int(detail.date[:4])

    entry = CollectionEntry(
        collection_id=collection.id,
        title=title,
        studio=studio,
        performers=performers,
        year=year,
        poster_path=poster,
        tpdb_id=tpdb_id,
        tpdb_kind=kind,
        match_state=MATCH_MATCHED,
        matched_at=utcnow(),
    )

    session.add(entry)
    session.flush()
    _link_library(session, entry)
    sync_to_tpdb(entry)

    return entry


def add_library_item(
    session: Session, collection: Collection, item_id: int
) -> CollectionEntry:
    """Add a title that is already in the library."""

    item = session.get(MediaItem, item_id)

    if item is None:
        raise CollectionError("No such library item")

    existing = _existing_entry(
        session,
        collection,
        tpdb_id=item.tpdb_id,
        external_id=item.adultempire_id,
        media_item_id=item.id,
    )

    if existing is not None:
        return existing

    entry = CollectionEntry(
        collection_id=collection.id,
        title=item.title or f"Item {item.id}",
        studio=item.site_name,
        performers=list(item.performers or []) or None,
        year=item.year,
        poster_path=item.poster_path,
        tpdb_id=item.tpdb_id,
        tpdb_kind="movie" if item.tpdb_id else None,
        external_source="adultempire" if item.adultempire_id else None,
        external_id=item.adultempire_id,
        match_state=MATCH_MATCHED if item.tpdb_id else MATCH_SELF_SOURCED,
        media_item_id=item.id,
    )

    session.add(entry)
    session.flush()

    if not entry.tpdb_id:
        enrich_entry(entry)

    sync_to_tpdb(entry)

    return entry


def add_catalogue_entry(
    session: Session, collection: Collection, source_entry: CollectionEntry
) -> CollectionEntry:
    """Copy a brochure or award entry into a user collection.

    The source row is left untouched -- it belongs to its catalogue, and a user
    collection that held references into other collections would break the
    moment a catalogue was re-synced.

    An Adult Empire entry gets a TPDB lookup here, which is the one place the
    add path spends a network request: the user asked for this title
    specifically, so it is worth the two round trips to bring it into the
    collection with the same artwork and ids a TPDB title arrives with.
    """

    existing = _existing_entry(
        session,
        collection,
        tpdb_id=source_entry.tpdb_id,
        external_id=source_entry.external_id,
        media_item_id=None,
    )

    if existing is not None:
        return existing

    entry = CollectionEntry(
        collection_id=collection.id,
        title=source_entry.title,
        studio=source_entry.studio,
        performers=list(source_entry.performers or []) or None,
        year=source_entry.year,
        released_at=source_entry.released_at,
        duration_minutes=source_entry.duration_minutes,
        rating=source_entry.rating,
        poster_path=source_entry.poster_path,
        tpdb_id=source_entry.tpdb_id,
        tpdb_kind=source_entry.tpdb_kind,
        external_source=source_entry.external_source,
        external_id=source_entry.external_id,
        match_state=(
            MATCH_MATCHED
            if source_entry.tpdb_id
            else MATCH_SELF_SOURCED
            if source_entry.external_id
            else MATCH_UNMATCHED
        ),
        media_item_id=source_entry.media_item_id,
    )

    session.add(entry)
    session.flush()

    if not entry.tpdb_id:
        enrich_entry(entry)

    _link_library(session, entry)
    sync_to_tpdb(entry)

    return entry


def sync_to_tpdb(entry: CollectionEntry) -> bool:
    """Mirror this title into the TPDB account's own collection, if enabled.

    A hard limitation, and it shapes what this can mean: **TPDB has exactly one
    collection per account**, a flat "collected" flag, with no notion of named
    lists. So local collections cannot be reproduced on TPDB; the most that can
    be mirrored is membership -- "this title is one I keep".

    It is also one-way. TPDB's ``user/collection`` route exposes GET, HEAD and
    POST but no DELETE, so removing a title from a local collection cannot
    un-collect it upstream. That has to be done on the TPDB website.

    Off by default for exactly those reasons.
    """

    if not settings_manager.settings.content.collections.sync_to_tpdb:
        return False

    if not entry.tpdb_id:
        return False

    api = client()

    if api is None:
        return False

    # The write is keyed on the *integer* id, not the uuid we store, and only
    # the detail record carries it.
    try:
        numeric_id = api.numeric_id(entry.tpdb_id, kind=entry.tpdb_kind or "movie")

        if numeric_id is None:
            return False

        if api.is_collected(numeric_id):
            return True

        return api.add_to_collection(numeric_id)
    except Exception as exc:
        # Never fails the add: the local collection is the source of truth and
        # the mirror is a convenience.
        logger.debug(f"TPDB collection sync failed for {entry.title!r}: {exc}")
        return False
