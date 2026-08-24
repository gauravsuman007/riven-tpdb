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

from kink import di
from loguru import logger
from sqlalchemy import select

from program.apis.tpdb_api import TpdbApi, TpdbApiError
from program.db.db import db_session
from program.media.item import MediaItem
from program.services.awards.matching import (
    MIN_TITLE_RATIO,
    best_match,
    evaluate_candidate,
    title_ratio,
)
from program.settings import settings_manager

# Same shortlist depth as award resolution: enough to cover a title TPDB lists
# under several editions, without spending a detail request on every hit.
DETAIL_CANDIDATES = 3


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

                item.indexed_at = datetime.now()
                session.commit()
                enriched += 1

                logger.debug(
                    f"Attached TPDB {match.tpdb_id} to {item.log_string} "
                    f"(score {match.score:.1f})"
                )

        return enriched

    def _match(self, item: MediaItem):
        """Find a TPDB record for a library item, or None.

        Uses the same two-pass shape as award resolution -- shortlist on title
        from the flat search response, then score the detail records -- and the
        same acceptance bar, because a wrong attachment here silently relabels
        an owned title.
        """

        results = self.api.search_movies_text(item.title, per_page=20) or []
        shortlist = sorted(
            (
                (title_ratio(item.title, r.title or ""), r)
                for r in results
                if r.id and title_ratio(item.title, r.title or "") >= MIN_TITLE_RATIO
            ),
            key=lambda pair: pair[0],
            reverse=True,
        )[:DETAIL_CANDIDATES]

        candidates = []

        for _ratio, result in shortlist:
            detail = self.api.get_movie(result.id)

            if detail is None:
                continue

            candidates.append(
                evaluate_candidate(
                    entry_title=item.title,
                    # site_name carries the Adult Empire studio for these items.
                    entry_studio=item.site_name,
                    entry_year=item.year,
                    # A storefront year is the release year already, unlike an
                    # award ceremony year.
                    year_offset=0,
                    entry_performers=list(item.performers or []),
                    tpdb_id=detail.id or result.id,
                    tpdb_kind="movie",
                    tpdb_title=detail.title,
                    tpdb_site=detail.site.name if detail.site else None,
                    tpdb_date=detail.date,
                    tpdb_performers=[p.name for p in detail.performers if p.name],
                    tpdb_poster=detail.poster
                    or (detail.posters.large if detail.posters else None),
                )
            )

        return best_match(candidates)
