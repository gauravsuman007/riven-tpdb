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

A page that carries no rating needs remembering too, and cannot be remembered
in the column: "nobody has reviewed this" and "we have not looked" are both
``rating IS NULL``, so without a record of the attempt every unreviewed title
is re-fetched on every run and the pending count never reaches zero. About a
third of products are in that state. They are recorded in a JSON sidecar --
derived, rebuildable, no migration -- exactly like the category index, and
``force`` re-checks them when a title has since been reviewed.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

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
from program.utils import data_dir_path

#: How many entries to enrich before committing. At the storefront's
#: one-request-per-second courtesy delay this is a commit every ~25 seconds,
#: which is often enough to watch and cheap enough not to matter.
BATCH_SIZE = 25

#: Where product ids whose page carried no rating are remembered.
UNRATED_FILENAME = "adultempire_unrated.json"


@dataclass
class BackfillProgress:
    """What a run has done so far. Read by the status endpoint while it runs."""

    running: bool = False
    considered: int = 0
    fetched: int = 0
    rated: int = 0
    #: Pages that were read and simply carry no rating -- nobody reviewed the
    #: title. Counted apart from `failed` because they are the normal case,
    #: not an error: roughly a third of product pages have no score.
    unrated: int = 0
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
            "unrated": self.unrated,
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
        self._unrated: set[str] | None = None

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

    # --- products known to carry no rating -------------------------------

    @property
    def unrated_path(self) -> Path:
        return data_dir_path / UNRATED_FILENAME

    @property
    def unrated(self) -> set[str]:
        """Product ids whose page was read and had no rating.

        Held apart from the database because the column cannot express it:
        `rating IS NULL` means both "nobody reviewed this" and "we have not
        looked", and conflating them re-fetches a third of the catalogue on
        every run forever.
        """

        if self._unrated is None:
            try:
                payload = json.loads(self.unrated_path.read_text(encoding="utf-8"))
                self._unrated = {str(pid) for pid in payload.get("products", [])}
            except FileNotFoundError:
                self._unrated = set()
            except (OSError, ValueError) as e:
                logger.warning(f"Could not read {self.unrated_path}; ignoring it: {e}")
                self._unrated = set()

        return self._unrated

    def _remember_unrated(self) -> None:
        try:
            self.unrated_path.parent.mkdir(parents=True, exist_ok=True)
            self.unrated_path.write_text(
                json.dumps(
                    {"checked_at": time.time(), "products": sorted(self.unrated)}
                ),
                encoding="utf-8",
            )
        except OSError as e:
            logger.error(f"Could not write {self.unrated_path}: {e}")

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

    @staticmethod
    def _product_id(entry: CollectionEntry) -> str | None:
        product = entry.adultempire_id or entry.external_id

        return str(product) if product else None

    def _to_check(self, entries, force: bool) -> list[CollectionEntry]:
        """Drop entries already known to have no rating, unless forcing."""

        if force:
            return list(entries)

        skip = self.unrated

        return [
            entry
            for entry in entries
            if (pid := self._product_id(entry)) is not None and pid not in skip
        ]

    def pending(self, force: bool = False) -> int:
        with db_session() as session:
            entries = session.execute(self._pending_query()).scalars().all()

            return len(self._to_check(entries, force))

    # --- the crawl -------------------------------------------------------

    def sync(self, limit: int = 0, force: bool = False) -> dict[str, object]:
        """Enrich pending entries. ``limit`` of 0 means all of them.

        ``force`` re-checks products already recorded as carrying no rating,
        which is how a title reviewed since the last run gets picked up.
        """

        with self._lock:
            if self.progress.running:
                logger.debug("A rating backfill is already running; not starting another.")

                return self.progress.snapshot()

            self.progress = BackfillProgress(running=True, started_at=time.time())

        try:
            return self._sync(limit, force)
        finally:
            self.progress.running = False
            self.progress.finished_at = time.time()

    @staticmethod
    def _propagate(session) -> int:
        """Push ratings entries already have onto the items they matched.

        `_apply` only touches a MediaItem when it writes a rating in that same
        run, so an entry rated before this service existed -- or by any other
        path -- left its library item showing nothing. One pass over the join
        rather than a special case inside the crawl, because the gap has
        nothing to do with crawling.
        """

        rows = (
            session.execute(
                select(CollectionEntry).where(
                    CollectionEntry.rating.isnot(None),
                    CollectionEntry.media_item_id.isnot(None),
                )
            )
            .unique()
            .scalars()
            .all()
        )

        filled = 0

        for entry in rows:
            item = entry.media_item

            # `not item.rating` and not `is None`: TPDB writes a literal 0,
            # which is not a score and must not block a real one.
            if item is not None and not item.rating:
                item.rating = entry.rating
                filled += 1

        if filled:
            logger.info(f"Copied {filled} entry ratings onto their library items.")

        return filled

    def _sync(self, limit: int, force: bool = False) -> dict[str, object]:
        with db_session() as session:
            self._propagate(session)
            session.commit()

            # Filtered in Python, not SQL: the set of already-checked products
            # lives in a file, and an `IN` clause over a few thousand ids to
            # avoid reading a few thousand rows is not a trade worth making.
            # The limit is applied after, so it counts titles that will
            # actually be fetched.
            entries = self._to_check(
                session.execute(self._pending_query()).scalars().all(), force
            )

            if limit:
                entries = entries[:limit]

            self.progress.considered = len(entries)

            if not entries:
                logger.info("No catalogue entries are waiting for a rating.")

                return self.progress.snapshot()

            logger.info(f"Backfilling ratings for {len(entries)} catalogue entries.")

            for index, entry in enumerate(entries, start=1):
                product_id = self._product_id(entry)

                if not product_id:
                    continue

                detail = self._detail(product_id)
                self.progress.fetched += 1
                self.progress.last_title = entry.title

                if detail is None:
                    self.progress.failed += 1
                elif self._apply(entry, detail):
                    self.progress.rated += 1
                    # A title can be reviewed after a run that found nothing.
                    self.unrated.discard(product_id)
                else:
                    self.progress.unrated += 1
                    # Remembered so it is not re-fetched forever: the column
                    # cannot tell "nobody reviewed it" from "not looked yet".
                    self.unrated.add(product_id)

                # Committed as we go. A run of several hundred titles is
                # minutes long; a single commit at the end would show no
                # progress and lose everything to a restart.
                if index % BATCH_SIZE == 0:
                    session.commit()
                    self._remember_unrated()

            session.commit()
            self._remember_unrated()

        logger.success(
            f"Rated {self.progress.rated} of {self.progress.considered} entries; "
            f"{self.progress.unrated} product pages carry no rating and "
            f"{self.progress.failed} could not be read."
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
