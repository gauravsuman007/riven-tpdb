"""Backfill audience ratings onto catalogue entries from Adult Empire.

The measured problem, which is why this exists at all:

* ``MediaItem.rating`` is **0 on every TPDB record** -- TPDB exposes no
  ranking, and a stored 0 is indistinguishable from "nobody has rated this".
* An Adult Empire **listing** page carries no rating either. 48 of 48 rows on
  the all-time-bestsellers page came back with ``rating=None``, which is why
  only 35 of 2,577 catalogue entries had one: those few were incidental.
* An Adult Empire **product** page does carry one -- ``rating-stars-avg``,
  out of five. Pirates 4.69, Island Fever 3 4.63, Pirates 2 4.88.

So the rating exists, one detail fetch away, for every entry that knows its
product id. The enabling detail: **the slug in a product URL is ignored**.
``/700215/`` and ``/700215/anything-porn-movies.html`` both return Pirates,
so an entry needs nothing but its numeric id -- no sitemap index, no slug
reconstruction, no title matching.

Progress is committed in batches rather than at the end. The category index
writes only when its whole crawl finishes, which makes a twelve-minute run
look like nothing is happening and throws the work away if it is interrupted;
this does not repeat that.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from loguru import logger
from sqlalchemy import or_, select

from program.db.db import db_session
from program.media.collection import CollectionEntry
from program.media.item import MediaItem
from program.services.recommendations.adultempire import (
    AdultEmpireClient,
    AdultEmpireError,
    RankedTitle,
)

#: How many entries to enrich before committing. At the storefront's
#: one-request-per-second courtesy delay this is a commit every ~25 seconds,
#: which is often enough to watch and cheap enough not to matter.
BATCH_SIZE = 25


@dataclass
class BackfillProgress:
    """What a run has done so far. Read by the status endpoint while it runs."""

    running: bool = False
    considered: int = 0
    fetched: int = 0
    rated: int = 0
    failed: int = 0
    started_at: float | None = None
    finished_at: float | None = None
    last_title: str | None = None

    def snapshot(self) -> dict[str, object]:
        return {
            "running": self.running,
            "considered": self.considered,
            "fetched": self.fetched,
            "rated": self.rated,
            "failed": self.failed,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "last_title": self.last_title,
        }


class RatingBackfill:
    """Fill in ``CollectionEntry.rating`` from Adult Empire product pages."""

    def __init__(self, client: AdultEmpireClient | None = None) -> None:
        self._client = client
        self._lock = threading.Lock()
        self.progress = BackfillProgress()

    @property
    def client(self) -> AdultEmpireClient:
        # Built on demand: constructing one opens a session, and this module is
        # imported by the router whether or not anything will ever crawl.
        if self._client is None:
            self._client = AdultEmpireClient()

        return self._client

    @property
    def running(self) -> bool:
        return self.progress.running

    # --- counting --------------------------------------------------------

    @staticmethod
    def _pending_query():
        """Entries that know an Adult Empire product and have no rating yet.

        ``external_id`` and ``adultempire_id`` are both checked because they
        mean different things: the first says the row *came from* the
        storefront, the second that a lookup *matched* it to a product. An
        award entry only ever has the second.
        """

        return select(CollectionEntry).where(
            CollectionEntry.rating.is_(None),
            or_(
                CollectionEntry.adultempire_id.isnot(None),
                CollectionEntry.external_source == "adultempire",
            ),
        )

    def pending(self) -> int:
        with db_session() as session:
            return len(session.execute(self._pending_query()).scalars().all())

    # --- the crawl -------------------------------------------------------

    def sync(self, limit: int = 0) -> dict[str, object]:
        """Enrich pending entries. ``limit`` of 0 means all of them."""

        with self._lock:
            if self.progress.running:
                logger.debug("A rating backfill is already running; not starting another.")

                return self.progress.snapshot()

            self.progress = BackfillProgress(running=True, started_at=time.time())

        try:
            return self._sync(limit)
        finally:
            self.progress.running = False
            self.progress.finished_at = time.time()

    def _sync(self, limit: int) -> dict[str, object]:
        with db_session() as session:
            query = self._pending_query()

            if limit:
                query = query.limit(limit)

            entries = session.execute(query).scalars().all()
            self.progress.considered = len(entries)

            if not entries:
                logger.info("No catalogue entries are waiting for a rating.")

                return self.progress.snapshot()

            logger.info(f"Backfilling ratings for {len(entries)} catalogue entries.")

            for index, entry in enumerate(entries, start=1):
                product_id = entry.adultempire_id or entry.external_id

                if not product_id:
                    continue

                detail = self._detail(str(product_id))
                self.progress.fetched += 1
                self.progress.last_title = entry.title

                if detail is None:
                    self.progress.failed += 1
                else:
                    if self._apply(entry, detail):
                        self.progress.rated += 1

                # Committed as we go. A run of several hundred titles is
                # minutes long; a single commit at the end would show no
                # progress and lose everything to a restart.
                if index % BATCH_SIZE == 0:
                    session.commit()

            session.commit()

        logger.success(
            f"Rated {self.progress.rated} of {self.progress.considered} entries "
            f"({self.progress.failed} pages carried no rating)."
        )

        return self.progress.snapshot()

    def _detail(self, product_id: str) -> RankedTitle | None:
        # The slug is ignored by the storefront, so the bare id is enough --
        # this is what makes a backfill possible without a title index.
        title = RankedTitle(
            product_id=product_id,
            title="",
            rank=0,
            listing="rating-backfill",
            url=f"/{product_id}/",
        )

        try:
            return self.client.enrich(title)
        except AdultEmpireError as e:
            logger.debug(f"Adult Empire product {product_id}: {e}")

            return None

    @staticmethod
    def _apply(entry: CollectionEntry, detail: RankedTitle) -> bool:
        """Copy what the product page knows onto the entry.

        Only ever fills gaps. An entry's own year or runtime came from the
        source that created it, and a storefront disagreeing with an award
        ballot is not grounds to overwrite the ballot.
        """

        if detail.rating is None:
            return False

        entry.rating = detail.rating

        if entry.year is None and detail.year:
            entry.year = detail.year

        if entry.duration_minutes is None and detail.duration_minutes:
            entry.duration_minutes = detail.duration_minutes

        # A library item shows the rating of the entry it was matched from.
        # Guarded on 0 as well as None because TPDB writes a literal 0 for
        # "no ranking", which would otherwise look like a real score of zero
        # and permanently block the real one.
        item: MediaItem | None = entry.media_item

        if item is not None and not item.rating:
            item.rating = detail.rating

        return True


#: Shared, because the progress it carries is what the status endpoint reads
#: while a run is in flight.
rating_backfill = RatingBackfill()
