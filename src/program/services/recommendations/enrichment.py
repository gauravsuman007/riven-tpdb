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
from program.media.collection import CollectionEntry
from program.media.item import MediaItem
from program.services.recommendations.metadata_lookup import (
    apply_match_poster,
    assign_provider_id,
    resolve_movie,
)
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
                        # A person cleared this one. Re-matching it would
                        # undo that decision on the next scheduled run --
                        # which is exactly what made detaching a wrong match
                        # useless before this existed.
                        MediaItem.tpdb_locked.is_(False),
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

                # The column depends on which provider answered. A StashDB
                # UUID in `tpdb_id` would make every TPDB lookup, dedupe and
                # "already in the library" check quietly wrong, and there
                # would be no way afterwards to tell where the value came
                # from.
                if not assign_provider_id(item, match):
                    continue

                # Not `if match.poster:`. A provider poster the newly
                # attached record does not vouch for has to GO, or the item
                # keeps the previous match's cover under the new match's id --
                # see `apply_match_poster`.
                if apply_match_poster(item, match) and not item.poster_path:
                    # Put the storefront's own cover back rather than leaving
                    # the item bare. It is the cover of this product id, so it
                    # is right whatever any metadata provider thinks, and the
                    # Adult Empire indexer would only restore it on a re-index
                    # that may never be asked for.
                    item.poster_path = _storefront_poster(session, item)

                item.indexed_at = utcnow()
                session.commit()
                enriched += 1

                logger.debug(
                    f"Attached {match.provider} {match.tpdb_id} to "
                    f"{item.log_string} (score {match.score:.1f})"
                )

        return enriched

    def _match(self, item: MediaItem):
        """Find a metadata record for a library item, or None.

        Delegates to the shared provider chain so this path and the
        collections path cannot drift apart -- and so the acceptance bar stays
        in one place, because a wrong attachment here silently relabels an
        owned title.

        The returned `Match` carries `provider`; read it before storing the id,
        because a StashDB UUID is not a TPDB id.
        """

        return resolve_movie(
            title=item.title,
            # site_name carries the Adult Empire studio for these items.
            studio=item.site_name,
            year=item.year,
            performers=list(item.performers or []),
            # A storefront year is the release year already, unlike an award
            # ceremony year.
            year_offset=0,
        )


def _storefront_poster(session, item: MediaItem) -> str | None:
    """The Adult Empire cover for this item's product id, if one is cached."""

    return session.execute(
        select(CollectionEntry.poster_path).where(
            CollectionEntry.external_id == item.adultempire_id,
            CollectionEntry.poster_path.is_not(None),
        )
    ).scalars().first()
