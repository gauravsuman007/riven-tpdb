"""Attaches TPDB metadata to titles that arrived without it.

Strictly additive and strictly after the fact. A brochure title is already
downloadable on Adult Empire's own metadata -- this only tries to find a
matching TPDB record afterwards so the item gains artwork, tags and a site id
like any other library title. Nothing here is on the critical path: if TPDB
never matches, the title still plays.

Runs against library items rather than collection entries, because the point is
to improve what is actually owned, not to resolve a catalogue nobody asked for.
"""

from datetime import datetime

from program.utils.time import utcnow

from kink import di
from loguru import logger
from sqlalchemy import select

from program.apis.tpdb_api import TpdbApi, TpdbApiError
from program.db.db import db_session
from program.media.item import MediaItem
from program.services.recommendations.tpdb_lookup import resolve_movie
from program.settings import settings_manager


class TpdbEnricher:
    """Best-effort TPDB backfill for library items lacking a tpdb_id."""

    def __init__(self) -> None:
        self.settings = settings_manager.settings.content.brochure
        self.initialized = False

        if not self.settings.enabled or not self.settings.enrich_from_tpdb:
            return

        if not settings_manager.settings.tpdb.api_token:
            logger.debug("TPDB enrichment needs an API token; skipping.")
            return

        self.api = di[TpdbApi]
        self.initialized = True

    def run(self, limit: int = 10) -> int:
        """Try to attach a TPDB record to up to ``limit`` items."""

        enriched = 0

        with db_session() as session:
            candidates = (
                session.execute(
                    select(MediaItem)
                    .where(
                        MediaItem.adultempire_id.is_not(None),
                        MediaItem.tpdb_id.is_(None),
                    )
                    .order_by(MediaItem.id.desc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )

            for item in candidates:
                try:
                    match = self._match(item)
                except TpdbApiError as exc:
                    logger.debug(f"TPDB unavailable during enrichment: {exc}")
                    break
                except Exception as exc:
                    logger.debug(f"Enrichment failed for {item.log_string}: {exc}")
                    continue

                if match is None:
                    # Deliberately not recorded as a failure. The next run will
                    # try again, and TPDB gains records over time.
                    continue

                item.tpdb_id = match.tpdb_id

                if match.poster:
                    item.poster_path = match.poster

                item.indexed_at = utcnow()
                session.commit()
                enriched += 1

                logger.debug(
                    f"Attached TPDB {match.tpdb_id} to {item.log_string} "
                    f"(score {match.score:.1f})"
                )

        return enriched

    def _match(self, item: MediaItem):
        """Find a TPDB record for a library item, or None.

        Delegates to the shared two-pass lookup so this path and the collections
        path cannot drift apart -- and so the acceptance bar stays in one place,
        because a wrong attachment here silently relabels an owned title.
        """

        return resolve_movie(
            self.api,
            title=item.title,
            # site_name carries the Adult Empire studio for these items.
            studio=item.site_name,
            year=item.year,
            performers=list(item.performers or []),
            # A storefront year is the release year already, unlike an award
            # ceremony year.
            year_offset=0,
        )
