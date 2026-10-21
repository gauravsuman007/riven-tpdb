"""A tracker's live daily .torrent download allowance, read from Prowlarr.

The PornoLab sidecar adds one fake release to every search that includes it:

    title    "PornoLab quota: ... left=48 limit=50 used=2 downloaded_bytes=... next_slot_s=0"
    seeders  downloads left today        leechers  the daily limit
    grabs    used today                  size      total downloaded, bytes

so the allowance is known from searches that run anyway, and nothing here is a
hand-kept count. This module:

* splits that row out before anything ranks or shows results (`split_quota`);
* remembers the latest one per indexer, plus a LOCAL reservation for grabs that
  are in flight or finished since it was read, so a burst of concurrent grabs
  cannot overshoot (`QuotaTracker`);
* decides whether a download may happen, and puts an indexer in cooldown after
  any failure instead of retrying.

Not covered, and not coverable from here: downloads made elsewhere with the
same account, a site-side window that differs from the sidecar's rolling 24h,
and group membership (unlimited on the site, invisible to the sidecar). Hence
the one-slot margin the automatic picker keeps.

If Prowlarr's cookies lapse the row degrades to "unavailable"; `unavailable_since`
is how a caller can tell and alert.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from loguru import logger

#: The marker in the fake release's title, after the indexer's base name.
QUOTA_MARKER = " quota:"

#: A common, cheap query. A nonsense one returns NO rows at all -- the sidecar
#: only adds its row when the tracker search returned something -- so the probe
#: must be a word the tracker has results for. Its releases are discarded.
PROBE_QUERY = "xxx"

#: The automatic picker leaves this many slots unspent for the owner.
RESERVE = 1

#: A row older than this is refreshed before a download is allowed.
STALE_SECONDS = 120

#: Never probe the same indexer more often than this.
MIN_REFRESH_SECONDS = 30

#: Back-off after a failed grab: first step, doubled each time, capped.
COOLDOWN_FIRST = 300
COOLDOWN_MAX = 3600

#: With the allowance unreadable, one cautious attempt per this long.
UNAVAILABLE_RETRY_SECONDS = 300


@dataclass(frozen=True)
class QuotaRow:
    left: int | None  # None when unavailable or unlimited
    limit: int | None
    used: int
    downloaded_bytes: int
    next_slot_s: int
    unavailable: bool
    unlimited: bool
    fetched_at: float = field(default_factory=time.time)


class QuotaStop(Exception):
    """A download is not allowed right now; the message is fit to show a user."""


def is_quota_title(title: str | None, indexer_name: str | None) -> bool:
    """Whether a result is the fake quota row of THIS indexer.

    Both halves matter: a real release could in principle contain "quota:", and
    a different indexer's row is not ours to act on.
    """

    if not title or not indexer_name:
        return False

    base = indexer_name.split(" (")[0].strip()

    return title.startswith(f"{base}{QUOTA_MARKER}")


def parse_row(title: str, seeders, leechers, grabs, size) -> QuotaRow:
    """Numbers come from the numeric fields; the title is read only for
    `next_slot_s` and the unlimited / unavailable cases. Unknown keys are
    ignored so the sidecar can add more. `seeders` is absent from the JSON when
    it is 0, so every field defaults."""

    pairs = dict(part.split("=", 1) for part in title.split() if "=" in part)
    unavailable = pairs.get("left") == "unknown"
    unlimited = pairs.get("left") == "-1"
    open_ended = unavailable or unlimited

    try:
        next_slot = int(pairs.get("next_slot_s", 0))
    except ValueError:
        next_slot = 0

    return QuotaRow(
        left=None if open_ended else int(seeders or 0),
        limit=None if open_ended else int(leechers or 0),
        used=int(grabs or 0),
        downloaded_bytes=int(size or 0),
        next_slot_s=next_slot,
        unavailable=unavailable,
        unlimited=unlimited,
    )


def split_quota(releases: list, indexer_name: str | None) -> tuple[list, QuotaRow | None]:
    """Remove the quota row from a result list, and return it parsed.

    Works on Prowlarr `ReleaseResource`s. The row's download link is refused
    with a 404, so it must never reach anything that would fetch it.
    """

    kept, row = [], None

    for release in releases:
        if is_quota_title(release.title, indexer_name):
            row = parse_row(
                release.title, release.seeders, release.leechers, release.grabs, release.size
            )
        else:
            kept.append(release)

    return kept, row


@dataclass
class _Reservation:
    started: float
    finished: float | None = None


class QuotaTracker:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.RLock()
        self._rows: dict[str, QuotaRow] = {}
        self._reserved: dict[str, list[_Reservation]] = {}
        self._cooldown_until: dict[str, float] = {}
        self._cooldown_step: dict[str, int] = {}
        self._last_refresh: dict[str, float] = {}
        self._last_blind_attempt: dict[str, float] = {}
        self._unavailable_since: dict[str, float] = {}
        self._refresher: Callable[[str], QuotaRow | None] | None = None

    @staticmethod
    def _key(name: str | None) -> str:
        return (name or "").lower()

    def register_refresher(self, refresher: Callable[[str], QuotaRow | None]) -> None:
        """How to re-read the row on demand: a cheap search by the scraper."""

        self._refresher = refresher

    def is_capped(self, name: str | None) -> bool:
        """Whether downloads from this indexer are limited, so a SEARCH must not
        spend any. Known from a row, or by name before the first row arrives
        (the first search must not be the one that burns a slot)."""

        key = self._key(name)

        with self._lock:
            return key in self._rows or "pornolab" in key

    def record(self, name: str | None, row: QuotaRow) -> None:
        key = self._key(name)

        with self._lock:
            self._rows[key] = row

            # Anything finished before this row was read is already in its
            # `used`; in-flight ones are not, and stay counted.
            self._reserved[key] = [
                r
                for r in self._reserved.get(key, [])
                if r.finished is None or r.finished > row.fetched_at
            ]

            if row.unavailable:
                self._unavailable_since.setdefault(key, row.fetched_at)
            else:
                self._unavailable_since.pop(key, None)

    def row(self, name: str | None) -> QuotaRow | None:
        with self._lock:
            return self._rows.get(self._key(name))

    def unavailable_since(self, name: str | None) -> float | None:
        """When the allowance became unreadable -- the signal to alert on."""

        with self._lock:
            return self._unavailable_since.get(self._key(name))

    def left_effective(self, name: str | None) -> int | None:
        with self._lock:
            row = self._rows.get(self._key(name))

            if row is None or row.left is None:
                return None

            return row.left - len(self._reserved.get(self._key(name), []))

    def _refresh(self, name: str, force: bool = False) -> None:
        key = self._key(name)
        now = self._clock()

        with self._lock:
            if self._refresher is None:
                return

            if not force and now - self._last_refresh.get(key, 0) < MIN_REFRESH_SECONDS:
                return

            self._last_refresh[key] = now
            refresher = self._refresher

        try:
            row = refresher(name)
        except Exception as e:
            logger.debug(f"Could not refresh the {name} quota: {e}")
            return

        if row is not None:
            self.record(name, row)

    def begin_grab(self, name: str, *, manual: bool) -> _Reservation:
        """Ask to download one .torrent. Returns a reservation to pass to
        `end_grab`, or raises `QuotaStop`."""

        key = self._key(name)
        now = self._clock()

        with self._lock:
            until = self._cooldown_until.get(key, 0)

        if now < until:
            raise QuotaStop(
                f"{name} refused a download recently, so downloads from it are "
                f"paused for another {_duration(until - now)}."
            )

        row = self.row(name)

        if row is None or now - row.fetched_at > STALE_SECONDS:
            self._refresh(name)
            row = self.row(name)

        with self._lock:
            if row is None or row.unavailable:
                # Policy: allow one cautious attempt now and then. The sidecar
                # still refuses at zero when it can read the profile.
                if now - self._last_blind_attempt.get(key, 0) < UNAVAILABLE_RETRY_SECONDS:
                    raise QuotaStop(
                        f"The {name} download allowance cannot be read right now "
                        "(its login may have lapsed in Prowlarr), and one "
                        "attempt was made recently. Re-test the indexer in "
                        "Prowlarr, then try again."
                    )

                self._last_blind_attempt[key] = now
            elif not row.unlimited:
                reserved = len(self._reserved.get(key, []))
                left = (row.left or 0) - reserved
                need = 1 if manual else 1 + RESERVE

                if left < need:
                    wait = max(0, row.next_slot_s - int(now - row.fetched_at))
                    when = f" The next slot opens in about {_duration(wait)}." if wait else ""
                    margin = "" if manual else f" (the automatic picker keeps {RESERVE} in reserve)"

                    raise QuotaStop(
                        f"{name} has {max(left, 0)} download(s) left today{margin}.{when}"
                    )

            reservation = _Reservation(started=now)
            self._reserved.setdefault(key, []).append(reservation)

            return reservation

    def end_grab(self, name: str, reservation: _Reservation, *, ok: bool) -> None:
        """Report how a download went. ANY failure pauses the indexer: the
        status Prowlarr passes back for a refusal is not reliable enough to
        parse, and retrying in a loop is how an allowance gets burned."""

        key = self._key(name)

        with self._lock:
            reservation.finished = self._clock()

            if ok:
                self._cooldown_step[key] = 0
                return

            step = min(self._cooldown_step.get(key, 0) * 2 or COOLDOWN_FIRST, COOLDOWN_MAX)
            self._cooldown_step[key] = step
            self._cooldown_until[key] = self._clock() + step

            logger.warning(f"A download from {name} failed; pausing downloads for {step}s")

        # Re-read the allowance so the next decision starts from the truth.
        self._refresh(name, force=False)


def _duration(seconds: float) -> str:
    seconds = int(max(0, seconds))

    if seconds >= 3600:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m"

    if seconds >= 60:
        return f"{seconds // 60}m"

    return f"{seconds}s"


tracker = QuotaTracker()
