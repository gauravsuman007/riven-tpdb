"""Indexer for titles sourced from the Adult Empire brochure.

The point of this indexer is that it makes no network call. A brochure entry
already carries everything the scrapers need -- title, studio, release year,
runtime and full cast -- so a requested title can go straight from "clicked" to
"being scraped" without waiting on a TPDB lookup that may not even find it.

TPDB enrichment is a separate, later, optional step (see
``services/recommendations/enrichment.py``): once the title is in the library
we try to attach a TPDB record for the artwork and tags, and nothing breaks if
that never succeeds.
"""

from datetime import datetime

from loguru import logger
from sqlalchemy import select

from program.core.runner import MediaItemGenerator, RunnerResult
from program.db.db import db_session
from program.media.collection import CollectionEntry
from program.media.item import MediaItem, Movie
from program.services.indexers.base import BaseIndexer

SOURCE = "adultempire"

_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    )
}


def parse_released(text: str | None) -> datetime | None:
    """Parse Adult Empire's "Sep 26 2005" release date."""

    if not text:
        return None

    parts = text.split()

    if len(parts) != 3:
        return None

    month = _MONTHS.get(parts[0][:3])

    if not month:
        return None

    try:
        return datetime(int(parts[2]), month, int(parts[1]))
    except ValueError:
        return None


def best_entry(session, external_id: str) -> CollectionEntry | None:
    """The richest cached brochure row for this product id.

    The same title appears in several listings -- trending and bestsellers
    overlap heavily -- but the rows are *not* interchangeable. Only shelves
    that have been through the detail-enrichment pass carry studio, cast and
    release date; a row first seen in an unenriched shelf holds nothing but a
    title. Taking whichever row the database happened to return first meant a
    manual scrape for "Pirates" could be handed a Movie with no site, no cast
    and no year, leaving the adult relevance filter no evidence to match on --
    so every candidate was rejected and the scrape came back empty.

    Ordering by how much metadata a row actually has makes the choice
    deterministic and always picks the most useful row available.
    """

    return session.execute(
        select(CollectionEntry)
        .where(
            CollectionEntry.external_source == SOURCE,
            CollectionEntry.external_id == external_id,
        )
        .order_by(
            CollectionEntry.studio.is_(None),
            CollectionEntry.performers.is_(None),
            CollectionEntry.released_at.is_(None),
            CollectionEntry.year.is_(None),
            CollectionEntry.id,
        )
    ).scalars().first()


def build_movie(entry: CollectionEntry) -> Movie:
    """Turn a brochure entry into a Movie, using only what the entry holds."""

    aired_at = entry.released_at

    if aired_at is None and entry.year:
        # Year alone still helps the scrapers' year check; January 1 is a
        # placeholder for "sometime that year", not a claimed release date.
        aired_at = datetime(entry.year, 1, 1)

    return Movie(
        {
            "adultempire_id": entry.external_id,
            "title": entry.title,
            "year": entry.year,
            "aired_at": aired_at,
            # The studio is the closest thing Adult Empire has to a TPDB site,
            # and the scrapers match on site_name.
            "site_name": entry.studio,
            "performers": list(entry.performers or []),
            "poster_path": entry.poster_path,
            "requested_by": "adultempire",
            "requested_at": datetime.now(),
        }
    )


class AdultEmpireIndexer(BaseIndexer):
    """Resolves an Adult Empire id from the cached brochure entry."""

    @classmethod
    def get_key(cls) -> str:
        return "adultempire_indexer"

    def run(self, item: MediaItem, log_msg: bool = True) -> MediaItemGenerator[Movie]:
        if not item.adultempire_id:
            logger.error(
                f"{item.log_string} has no adultempire_id, cannot index it"
            )
            return

        if item.type not in ("movie", "mediaitem"):
            logger.debug(f"Adult Empire indexer skipping {item.log_string}")
            return

        entry = self._entry_for(item.adultempire_id)

        if entry is None:
            logger.warning(
                f"No brochure entry cached for Adult Empire id "
                f"{item.adultempire_id}; cannot index without re-syncing"
            )
            return

        if item.type == "mediaitem":
            indexed = self.copy_items(item, build_movie(entry))
            indexed.indexed_at = datetime.now()

            if log_msg:
                logger.debug(
                    f"Indexed {indexed.log_string} from Adult Empire "
                    f"({item.adultempire_id}) with no TPDB lookup"
                )

            yield RunnerResult(media_items=[indexed])
            return

        if isinstance(item, Movie):
            self._apply(item, entry)
            item.indexed_at = datetime.now()
            yield RunnerResult(media_items=[item])

    @staticmethod
    def _entry_for(external_id: str) -> CollectionEntry | None:
        """The cached brochure row for this product id."""

        with db_session() as session:
            entry = best_entry(session, external_id)

            if entry is not None:
                session.expunge(entry)

            return entry

    @staticmethod
    def _apply(item: Movie, entry: CollectionEntry) -> None:
        """Refresh an existing Movie without clobbering better data.

        A title may have been enriched from TPDB since it was requested; the
        brochure is the fallback source, not the authority, so it only fills
        gaps.
        """

        item.title = item.title or entry.title
        item.year = item.year or entry.year
        item.site_name = item.site_name or entry.studio
        item.poster_path = item.poster_path or entry.poster_path

        if not item.performers and entry.performers:
            item.performers = list(entry.performers)
