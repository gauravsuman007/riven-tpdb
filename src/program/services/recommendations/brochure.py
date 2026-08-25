"""Keeps the Adult Empire brochure cached locally as collections.

The brochure is browsed, not searched, so it has to be fast: fetching listings
on page load would mean a rate-limited round trip per shelf. Instead each
listing is mirrored into a :class:`Collection` and served from the database.

Two phases again, for the same reason as the awards service but with different
economics:

    * :meth:`sync_listings` reads the ranked pages. Cheap -- 48 titles per
      request -- and it is what keeps rank and cover art current.
    * :meth:`enrich_batch` fills in rating, studio, cast and runtime, which
      only the detail page carries. One request per title, so it is bounded
      per run and resumable.

Entries land as ``self_sourced``: unlike an award title, a brochure entry needs
no TPDB lookup to be actionable. It is requestable the moment it is cached.
"""

from datetime import datetime

from loguru import logger
from sqlalchemy import select

from program.db.db import db_session
from program.media.collection import (
    MATCH_SELF_SOURCED,
    Collection,
    CollectionEntry,
)
from program.services.recommendations.adultempire import (
    LISTINGS,
    AdultEmpireClient,
    AdultEmpireError,
)
from program.services.indexers.adultempire_indexer import parse_released
from program.services.recommendations.tpdb_lookup import enrich_entry
from program.settings import settings_manager

SOURCE = "adultempire"

# Display names and the order the brochure shows them in. The mapping is here
# rather than in the client because it is presentation, not protocol.
SHELVES: list[tuple[str, str, str]] = [
    (
        "all-time-bestsellers",
        "All-Time Bestsellers",
        "The catalogue's most-sold titles of all time.",
    ),
    ("trending", "Trending Now", "What is selling this week."),
    ("bestsellers", "Current Bestsellers", "Top sellers right now."),
    ("new-releases", "New Releases", "Just added to the catalogue."),
]


def collection_key(listing: str) -> str:
    return f"{SOURCE}-{listing}"


class BrochureService:
    """Mirrors Adult Empire's ranked listings into local collections."""

    def __init__(self) -> None:
        self.settings = settings_manager.settings.content.brochure
        self.initialized = False

        if not self.settings.enabled:
            return

        self.client = AdultEmpireClient()
        self.initialized = True
        logger.success("Adult Empire brochure initialized!")

    # ------------------------------------------------------------- listings

    def sync_listings(self) -> int:
        """Refresh every shelf. Returns the number of entries written."""

        pages = self.settings.pages_per_listing
        written = 0

        for listing, name, description in SHELVES:
            if listing not in LISTINGS:
                continue

            try:
                items = self.client.listing(listing, pages=pages)
            except AdultEmpireError as exc:
                logger.warning(f"Adult Empire listing {listing} failed: {exc}")
                continue

            if not items:
                logger.warning(f"Adult Empire listing {listing} came back empty")
                continue

            written += self._store(listing, name, description, items)

        logger.info(f"Adult Empire brochure synced: {written} entries")

        return written

    def _store(self, listing, name, description, items) -> int:
        with db_session() as session:
            collection = session.execute(
                select(Collection).where(Collection.key == collection_key(listing))
            ).scalar_one_or_none()

            if collection is None:
                collection = Collection(
                    key=collection_key(listing),
                    source=SOURCE,
                    name=name,
                    description=description,
                )
                session.add(collection)
                session.flush()

            existing = {
                entry.external_id: entry
                for entry in session.execute(
                    select(CollectionEntry).where(
                        CollectionEntry.collection_id == collection.id
                    )
                ).scalars()
            }

            for item in items:
                entry = existing.get(item.product_id)

                if entry is None:
                    entry = CollectionEntry(
                        collection_id=collection.id,
                        external_source=SOURCE,
                        external_id=item.product_id,
                        title=item.title,
                        match_state=MATCH_SELF_SOURCED,
                    )
                    session.add(entry)

                # Rank and cover move between runs; everything else is only
                # filled in by enrichment and must not be blanked here.
                entry.rank = item.rank
                entry.title = item.title or entry.title
                entry.category = listing

                if item.poster:
                    entry.poster_path = item.poster

            # A title that fell off the listing should not linger at a stale
            # rank -- unless it was requested, in which case the row is the
            # only record of where it came from.
            fresh = {item.product_id for item in items}

            for external_id, entry in existing.items():
                if external_id not in fresh and entry.media_item_id is None:
                    session.delete(entry)

            collection.refreshed_at = datetime.now()
            collection.name = name
            collection.description = description
            session.commit()

        return len(items)

    # ----------------------------------------------------------- enrichment

    def enrich_batch(self, limit: int | None = None) -> int:
        """Fill in rating, studio, cast and runtime for un-enriched entries.

        "Un-enriched" is `rating is null`, which is the field only the detail
        page provides. Highest-ranked first: those are the ones a brochure
        shelf actually shows.
        """

        limit = limit or self.settings.enrich_batch_size
        done = 0

        with db_session() as session:
            pending = (
                session.execute(
                    select(CollectionEntry)
                    .join(Collection)
                    .where(
                        Collection.source == SOURCE,
                        CollectionEntry.rating.is_(None),
                        CollectionEntry.external_id.is_not(None),
                    )
                    .order_by(CollectionEntry.rank)
                    .limit(limit)
                )
                .scalars()
                .all()
            )

            for entry in pending:
                from program.services.recommendations.adultempire import RankedTitle

                # The bare "/{id}/" form, not "/{id}/{slug}.html". A wrong
                # slug still answers 200 but serves a page with none of the
                # product markup on it, so enrichment would quietly find
                # nothing at all.
                probe = RankedTitle(
                    product_id=entry.external_id or "",
                    title=entry.title,
                    rank=entry.rank or 0,
                    listing=entry.category or "",
                    url=f"/{entry.external_id}/",
                )

                try:
                    detail = self.client.enrich(probe)
                except AdultEmpireError as exc:
                    logger.warning(f"Adult Empire unavailable, pausing: {exc}")
                    break

                entry.rating = detail.rating
                entry.studio = detail.studio or entry.studio
                entry.year = detail.year or entry.year
                entry.duration_minutes = detail.duration_minutes
                entry.released_at = parse_released(detail.released)

                if detail.performers:
                    entry.performers = detail.performers

                if detail.poster and not entry.poster_path:
                    entry.poster_path = detail.poster

                # Per entry: enrichment is a rate-limited request each, and a
                # batch is minutes of work to throw away on a crash.
                session.commit()
                done += 1

        if done:
            logger.debug(f"Enriched {done} Adult Empire entries")

        return done

    # ----------------------------------------------------- TPDB resolution

    def resolve_batch(self, limit: int | None = None) -> int:
        """Attach a TPDB record to catalogue entries that have none yet.

        Separate from :meth:`enrich_batch`, which reads Adult Empire's own
        detail page. This one asks TPDB whether the title exists there too,
        and it has to run over the *catalogue* rather than over library items:
        the detail page decides which view to render from ``tpdb_id``, so an
        entry nobody has requested yet still needs resolving or it is stuck on
        the storefront view forever.

        Until this existed the lookup only ran when a title was requested,
        which left every un-requested entry unresolved -- 573 of 576 of them,
        measured -- and made the TPDB detail page look like it only worked for
        new titles.

        Bounded per run and resumable: ``resolve_movie`` costs a search plus up
        to three detail requests, and TPDB allows two a second.
        """

        if not self.settings.enrich_from_tpdb:
            return 0

        limit = limit or self.settings.resolve_batch_size
        done = 0

        with db_session() as session:
            pending = (
                session.execute(
                    select(CollectionEntry)
                    .join(Collection)
                    # Never attempted, rather than "has no tpdb_id". About
                    # one title in five legitimately has no TPDB record --
                    # a bare one-word title, or a pre-1980 release the matcher
                    # correctly refuses to guess at -- and keying off the id
                    # alone would re-ask TPDB about those on every single run,
                    # forever, spending the whole rate limit on known misses.
                    .where(
                        Collection.source == SOURCE,
                        CollectionEntry.tpdb_id.is_(None),
                        CollectionEntry.matched_at.is_(None),
                    )
                    # Highest-ranked first: those are the ones on screen.
                    .order_by(CollectionEntry.rank)
                    .limit(limit)
                )
                .scalars()
                .all()
            )

            for entry in pending:
                if enrich_entry(entry):
                    done += 1
                else:
                    # Stamp the attempt so the query above does not pick this
                    # entry up again. `match_state` deliberately stays
                    # `self_sourced`: the title is still downloadable from the
                    # storefront's own metadata, and demoting it to
                    # `unmatched` would make `actionable` false and take away
                    # a title that works.
                    entry.matched_at = datetime.now()

                # Per entry, for the same reason as enrichment: a batch is
                # minutes of rate-limited work to lose on a crash.
                session.commit()

        if done:
            logger.debug(f"Resolved {done} Adult Empire entries to TPDB")

        return done
