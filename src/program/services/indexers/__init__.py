"""Adult-only indexer. Resolves TPDB and Adult Empire items into Riven Movies.

This fork drops TMDB/TVDB indexing entirely. Two adult identifiers are
recognised and they work differently:

    * ``tpdb_id`` is resolved against TPDB, which supplies full metadata.
    * ``adultempire_id`` is resolved from the cached brochure entry, with no
      network call at all -- the brochure already carries title, studio, year,
      runtime and cast, which is everything the scrapers need. This is what
      lets a brochure title start downloading immediately instead of waiting
      on a TPDB lookup that might not find it.

Anything mainstream (IMDB/TMDB/TVDB) is skipped.
"""

from loguru import logger
from sqlalchemy import select

from program.db.db import db_session
from program.media.item import MediaItem, Movie
from program.media.state import States
from program.services.indexers.base import BaseIndexer
from program.services.indexers.adultempire_indexer import AdultEmpireIndexer
from program.services.indexers.tpdb_indexer import TPDBIndexer
from program.core.runner import MediaItemGenerator


class IndexerService(BaseIndexer):
    """Entry point to indexing. Adult-only: delegates to the TPDB indexer."""

    def __init__(self):
        super().__init__()

        self.tpdb_indexer = TPDBIndexer()
        self.adultempire_indexer = AdultEmpireIndexer()

    @classmethod
    def get_key(cls) -> str:
        return "indexer"

    def run(
        self,
        item: MediaItem,
        log_msg: bool = True,
    ) -> MediaItemGenerator:
        """Route to the indexer that matches the item's identifier.

        TPDB wins when both are present: it is the richer source, and an item
        that has already been matched to a TPDB record should not fall back to
        the sparser brochure data on a reindex.
        """

        if item.tpdb_id:
            yield from self.tpdb_indexer.run(
                item=item,
                log_msg=log_msg,
            )
            return

        if item.adultempire_id:
            yield from self.adultempire_indexer.run(
                item=item,
                log_msg=log_msg,
            )
            return

        logger.debug(
            f"Skipping item with no adult identifier (adult-only fork): "
            f"{item.log_string}"
        )
        return

    def reindex_ongoing(self) -> int:
        """Reindex all ongoing/unreleased movies (adult-only: movies only)."""

        try:
            with db_session() as session:
                items = (
                    session.execute(
                        select(Movie).where(
                            Movie.last_state.in_(
                                [States.Ongoing, States.Unreleased]
                            )
                        )
                    )
                    .unique()
                    .scalars()
                    .all()
                )

                if not items:
                    logger.debug("No ongoing/unreleased items to reindex")
                    return 0

                logger.debug(f"Reindexing {len(items)} ongoing/unreleased items")

                count = 0

                for item in items:
                    try:
                        updated = next(self.run(item, log_msg=False), None)

                        if updated:
                            with session.no_autoflush:
                                session.merge(updated)

                            count += 1
                    except Exception as e:
                        logger.error(f"Failed reindexing {item.log_string}: {e}")
                        continue

                try:
                    session.commit()
                except Exception as e:
                    logger.debug(
                        f"Commit failed during reindex (likely item was deleted): {e}"
                    )
                    session.rollback()

                return count
        except Exception as e:
            logger.error(f"Error during reindex_ongoing: {e}")
            return 0
