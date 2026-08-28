"""One-time repair for CollectionEntry rows a fresh brochure/awards request left
unlinked before `_link_collection_entries` (program/db/db_functions.py) existed.

Before that fix, `media_item_id` was never set for a title requested straight
from the brochure (or auto-requested via awards): the MediaItem row was only
created later, by the Indexer, with nothing revisiting the CollectionEntry
afterwards. That is why a brochure detail page looked permanently stuck on
"Request" and why the Library page 404'd for the same title once it was
opened elsewhere. New requests are fixed going forward; this repairs whatever
already exists in the database.

Run once, inside the container:

    docker exec riven-tpdb env PYTHONPATH=/riven/src \\
        /riven/.venv/bin/python -m program.services.collections.backfill_media_item_links
"""

from loguru import logger
from sqlalchemy import or_, select

from program.db.db import db_session
from program.db.db_functions import _link_collection_entries
from program.media.collection import CollectionEntry
from program.media.item import MediaItem


def run() -> int:
    """Link every unlinked CollectionEntry to its MediaItem, if one exists.

    Returns the number of entries linked.
    """

    linked = 0

    with db_session() as session:
        unlinked = (
            session.execute(
                select(CollectionEntry).where(
                    CollectionEntry.media_item_id.is_(None),
                    or_(
                        CollectionEntry.tpdb_id.is_not(None),
                        CollectionEntry.external_id.is_not(None),
                    ),
                )
            )
            .scalars()
            .all()
        )

        logger.info(f"Checking {len(unlinked)} unlinked collection entries")

        for entry in unlinked:
            conditions = []

            if entry.tpdb_id:
                conditions.append(MediaItem.tpdb_id == entry.tpdb_id)

            if entry.external_id:
                conditions.append(MediaItem.adultempire_id == entry.external_id)

            match = (
                session.execute(select(MediaItem).where(or_(*conditions)))
                .scalars()
                .first()
            )

            if match is not None:
                # Reuse the real linking function rather than setting
                # media_item_id directly -- it also catches any *other*
                # entries waiting on the same title, not just this one.
                _link_collection_entries(session, match)
                linked += 1
                logger.debug(f"Linked {entry.title!r} -> MediaItem {match.id}")

        session.commit()

    logger.info(f"Backfill complete: linked {linked} of {len(unlinked)} entries")

    return linked


if __name__ == "__main__":
    run()
