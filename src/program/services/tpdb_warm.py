"""Keep every library title's TPDB data ready before anyone opens it.

A title's detail page needs three TPDB answers: the record itself, its similar
titles and whether it is collected. TPDB takes 5-15 seconds for each, so a
library title whose cached answers had expired cost that on every open.

This pins the library's ids in the client (pinned answers are never evicted,
and expired ones are served at once while they refresh) and fetches whatever
is missing, here in the background rather than in a page load. Measured before
this: 8.8s for the record, 8.6s for the related row and 7.7s for the collected
flag on a cold library title; after, each is a cache read.

It also fills a library title's missing cast and studio from that record. A
title requested from the Adult Empire storefront is built from the storefront
row, which often has neither; matching it to TPDB later set its ``tpdb_id``
and nothing else. "After Dark" sat in the library with no cast at all, so
clicking Mia Malkova on its own page listed her other titles but not it.
"""

from __future__ import annotations

from kink import di
from loguru import logger
from sqlalchemy import select

from program.apis.tpdb_api import TpdbApi, TpdbApiError
from program.db.db import db_session
from program.services.indexers.tpdb_mapping import _performer_names, _site
from program.settings import settings_manager


def library_tpdb_ids() -> list[str]:
    from program.media.item import MediaItem

    with db_session() as session:
        rows = session.execute(
            select(MediaItem.tpdb_id).where(MediaItem.tpdb_id.is_not(None)).distinct()
        )

        return [str(row) for row in rows.scalars() if row]


def warm_library_tpdb() -> None:
    """Pin and fill the TPDB answers for every title in the library."""

    if not settings_manager.settings.tpdb.enabled:
        return

    try:
        api = di[TpdbApi]
    except Exception:
        return

    ids = library_tpdb_ids()
    api.pinned.update(ids)

    warmed = sum(1 for uuid in ids if warm_title(api, uuid))

    logger.debug(f"TPDB data ready for {warmed} of {len(ids)} library titles")


def warm_title(api: TpdbApi, uuid: str) -> bool:
    """Pin one title and fill its record, related row and collected flag."""

    api.pinned.add(uuid)

    try:
        # Movie first, scene second: both lookups are cached either way (404s
        # included), so the detail page's own probe for the other kind is also
        # answered locally from now on.
        record = api.get_movie(uuid)
        kind = "movie"

        if record is None:
            record = api.get_scene(uuid)
            kind = "scene"

        if record is None:
            return False

        fill_gaps(uuid, record.model_dump())

        # Read from the cached payload: the parsed model cannot carry `_id`.
        numeric = api.numeric_id(uuid, kind)

        if kind == "movie":
            api.get_similar_movies(uuid)
        else:
            api.get_similar_scenes(uuid)

        if numeric:
            api.pinned.add(str(numeric))
            api.is_collected(numeric)

        return True
    except TpdbApiError as exc:
        # One title failing (or TPDB having a bad minute) is no reason to stop
        # warming the rest; the next run picks it up.
        logger.debug(f"Could not warm TPDB data for {uuid}: {exc}")
        return False


def fill_gaps(uuid: str, record: dict) -> int:
    """Give library titles with this TPDB id the cast and studio they lack.

    Gaps only, never a replacement: a cast or studio already present came
    from somewhere deliberate (the indexer, a manual match), and the same
    restraint keeps a wrong match from overwriting good data -- see "A wrong
    match must not be able to destroy correct artwork" in AGENTS.md.

    Loaded and assigned through the ORM rather than a bulk UPDATE: the
    library's cast search table (``ItemPerformer``) is kept in step by mapper
    events, which a bulk statement skips.
    """

    from program.media.item import MediaItem

    performers = _performer_names(record)
    _, site_name = _site(record)

    if not performers and not site_name:
        return 0

    filled = 0

    with db_session() as session:
        items = session.execute(
            select(MediaItem).where(MediaItem.tpdb_id == uuid)
        ).scalars().all()

        for item in items:
            changed = False

            if performers and not item.performers:
                item.performers = performers
                changed = True

            if site_name and not item.site_name:
                item.site_name = site_name
                changed = True

            filled += changed

        if filled:
            session.commit()
            logger.debug(f"Filled cast/studio from TPDB for {filled} item(s) of {uuid}")

    return filled
