"""Adult-only indexer that resolves TPDB items into Riven Movies.

This fork drops TMDB/TVDB indexing entirely: only items carrying a TPDB id
are resolved. Any mainstream (IMDB/TMDB/TVDB) item is skipped.
"""

from loguru import logger
from sqlalchemy import select

from program.db.db import db_session
from program.media.item import MediaItem, Movie
from program.media.state import States
from program.services.indexers.base import BaseIndexer
from program.services.indexers.tpdb_indexer import TPDBIndexer
from program.core.runner import MediaItemGenerator


class IndexerService(BaseIndexer):
    """Entry point to indexing. Adult-only: delegates to the TPDB indexer."""

    def __init__(self):
        super().__init__()

        self.tpdb_indexer = TPDBIndexer()

    @classmethod
    def get_key(cls) -> str:
        return "indexer"

    def run(
        self,
        item: MediaItem,
        log_msg: bool = True,
    ) -> MediaItemGenerator:
        """Run the TPDB indexer for TPDB items; skip anything mainstream."""

        if item.tpdb_id:
            yield from self.tpdb_indexer.run(
                item=item,
                log_msg=log_msg,
            )
            return

        logger.debug(
            f"Skipping non-TPDB item (adult-only fork): {item.log_string}"
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
