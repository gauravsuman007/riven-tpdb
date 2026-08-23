"""Tests for uncached-download support and infohash URL resolution.

Upstream Riven is cached-only: a torrent the debrid provider does not already
hold is blacklisted and the item stalls. Adult releases are almost never in a
provider's cache, so this fork asks the provider to fetch one instead and
reschedules the item. These tests cover that path and the two helpers it
depends on, plus the free short-circuit in ``get_infohash_from_url``.

Run as a script, like the other tests in this fork:

    python src/tests/test_uncached_downloads.py
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

# Registers the custom loguru levels (DEBRID, SCRAPER, ...) that the code
# under test logs to; without it logger.log("DEBRID", ...) raises ValueError.
import program.utils.logging  # noqa: E402,F401

from program.services.downloaders import (  # noqa: E402
    _format_progress,
    _has_expired,
    Downloader,
)

PASSED = []
FAILED = []


def check(name, fn):
    try:
        fn()
    except AssertionError as exc:
        FAILED.append((name, str(exc)))
    except Exception as exc:  # noqa: BLE001
        FAILED.append((name, f"{type(exc).__name__}: {exc}"))
    else:
        PASSED.append(name)


class StubService:
    """Minimal stand-in for a debrid downloader."""

    def __init__(self, key="torbox", torrent_id="t1", info=None, add_raises=None):
        self.key = key
        self._torrent_id = torrent_id
        self._info = info
        self._add_raises = add_raises
        self.added = []

    def add_torrent(self, infohash):
        self.added.append(infohash)
        if self._add_raises:
            raise self._add_raises
        return self._torrent_id

    def get_torrent_info(self, torrent_id):
        if self._info is None:
            raise RuntimeError("no info")
        return self._info


def make_downloader(services, poll=10, max_wait=24):
    """Bind the real method to a stub, avoiding a full service construction."""

    stub = SimpleNamespace(
        initialized_services=services,
        uncached_poll_minutes=poll,
        uncached_max_wait_hours=max_wait,
        _uncached_since={},
        UNCACHED_STREAMS_PER_PASS=3,
    )
    stub._request_uncached = Downloader._request_uncached.__get__(stub, Downloader)
    stub._uncached_wait_started = Downloader._uncached_wait_started.__get__(
        stub, Downloader
    )
    return stub


def _stream(infohash="abc123", raw_title="Some Release 1080p"):
    return SimpleNamespace(infohash=infohash, raw_title=raw_title)


def _item():
    return SimpleNamespace(log_string="Some Movie (2021)", id=42)


# --- _format_progress ------------------------------------------------------


def _test_progress_fraction_scale():
    assert _format_progress(0.0) == "0%", _format_progress(0.0)
    assert _format_progress(0.5) == "50%", _format_progress(0.5)
    assert _format_progress(1.0) == "100%", _format_progress(1.0)


def _test_progress_percent_scale():
    """Providers that already report 0-100 must not render as 8300%."""

    assert _format_progress(83.0) == "83%", _format_progress(83.0)
    assert _format_progress(100.0) == "100%", _format_progress(100.0)


def _test_progress_clamped():
    assert _format_progress(150.0) == "100%", _format_progress(150.0)
    assert _format_progress(-5.0) == "0%", _format_progress(-5.0)


# --- _has_expired ----------------------------------------------------------


def _test_expiry_naive_recent():
    assert _has_expired(datetime.now() - timedelta(hours=1), 24) is False


def _test_expiry_naive_old():
    assert _has_expired(datetime.now() - timedelta(hours=48), 24) is True


def _test_expiry_timezone_aware():
    """Providers send tz-aware stamps; comparing to naive now() raises."""

    aware = datetime.now(timezone.utc) - timedelta(hours=1)
    assert _has_expired(aware, 24) is False

    aware_old = datetime.now(timezone.utc) - timedelta(hours=48)
    assert _has_expired(aware_old, 24) is True


def _test_expiry_garbage_is_not_expired():
    """An unreadable stamp must not throw away a live download."""

    assert _has_expired("not-a-datetime", 24) is False  # type: ignore[arg-type]
    assert _has_expired(None, 24) is False  # type: ignore[arg-type]


# --- _request_uncached -----------------------------------------------------


def _test_requests_fetch_and_reschedules():
    info = SimpleNamespace(progress=0.25, created_at=datetime.now())
    svc = StubService(info=info)
    dl = make_downloader([svc], poll=10)

    before = datetime.now()
    result = dl._request_uncached(_item(), [_stream("hash-one")])

    assert result is not None, "should reschedule"
    assert svc.added == ["hash-one"], svc.added
    delta = (result - before).total_seconds()
    assert 500 < delta < 700, delta


def _test_picks_best_ranked_stream():
    svc = StubService(info=SimpleNamespace(progress=0.0, created_at=datetime.now()))
    dl = make_downloader([svc])

    dl._request_uncached(_item(), [_stream("best"), _stream("worse")])

    assert svc.added == ["best"], svc.added


def _test_gives_up_when_provider_stalled():
    """Past the max wait the provider is not going to deliver."""

    info = SimpleNamespace(progress=0.0, created_at=datetime.now())
    dl = make_downloader([StubService(info=info)], max_wait=24)

    item, stream = _item(), _stream()
    # We first asked 48h ago.
    dl._uncached_since[(item.id, stream.infohash)] = datetime.now() - timedelta(
        hours=48
    )

    assert dl._request_uncached(item, [stream]) is None


def _test_old_provider_torrent_does_not_expire_us():
    """Re-adding an infohash returns the account's *existing* torrent.

    If that torrent was added months ago -- or has since expired -- its
    created_at is far past the deadline, and keying the wait on it would make
    the very first attempt give up immediately.
    """

    info = SimpleNamespace(
        progress=0.0, created_at=datetime.now() - timedelta(days=200)
    )
    dl = make_downloader([StubService(info=info)], max_wait=24)

    assert dl._request_uncached(_item(), [_stream()]) is not None


def _test_queued_response_is_accepted():
    """A brand-new torrent comes back queued, with no torrent id.

    Treating that as an error made every genuinely new torrent look like an
    immediate failure.
    """

    from program.services.downloaders.torbox import TorBoxQueued

    svc = StubService(add_raises=TorBoxQueued(4067910))
    dl = make_downloader([svc])

    result = dl._request_uncached(_item(), [_stream()])

    assert result is not None, "queued must count as accepted"
    assert svc.added == ["abc123"], svc.added


def _test_transient_provider_error_retries_not_gives_up():
    """A provider error inside the budget must not strand the item.

    TorBox intermittently answers 400 "Torrent file is not valid" for a magnet
    whose metadata it has not resolved yet, then accepts the same hash moments
    later. Treating that as terminal blacklisted good streams outright.
    """

    dl = make_downloader([StubService(add_raises=RuntimeError("[400] boom"))])

    assert dl._request_uncached(_item(), [_stream()]) is not None


def _test_gives_up_only_when_every_candidate_expired():
    dl = make_downloader([StubService(add_raises=RuntimeError("boom"))], max_wait=24)
    item = _item()
    streams = [_stream("a"), _stream("b")]

    for st in streams:
        dl._uncached_since[(item.id, st.infohash)] = datetime.now() - timedelta(
            hours=48
        )

    assert dl._request_uncached(item, streams) is None


def _test_falls_through_to_next_stream():
    """One unusable torrent must not hide the others."""

    class PickyService(StubService):
        def add_torrent(self, infohash):
            self.added.append(infohash)
            if infohash == "bad":
                raise RuntimeError("[400] Torrent file is not valid")
            return "t9"

    svc = PickyService(
        info=SimpleNamespace(progress=0.3, created_at=datetime.now())
    )
    dl = make_downloader([svc])

    assert dl._request_uncached(_item(), [_stream("bad"), _stream("good")]) is not None
    assert svc.added == ["bad", "good"], svc.added


def _test_only_considers_top_n_streams():
    svc = StubService(add_raises=RuntimeError("boom"))
    dl = make_downloader([svc])

    dl._request_uncached(_item(), [_stream(f"h{i}") for i in range(10)])

    assert len(svc.added) == 3, svc.added


def _test_wait_clock_is_stable_across_polls():
    """The clock must start once, not reset on every poll."""

    dl = make_downloader(
        [StubService(info=SimpleNamespace(progress=0.0, created_at=datetime.now()))]
    )
    item, stream = _item(), _stream()

    first = dl._uncached_wait_started(item, stream)
    second = dl._uncached_wait_started(item, stream)

    assert first == second, (first, second)


def _test_unreadable_progress_still_waits():
    """Accepting the torrent is what matters; progress is best-effort."""

    svc = StubService(info=None)  # get_torrent_info raises
    dl = make_downloader([svc])

    assert dl._request_uncached(_item(), [_stream()]) is not None
    assert svc.added == ["abc123"], svc.added


def _test_falls_through_to_next_service():
    bad = StubService(key="a", add_raises=RuntimeError("nope"))
    good = StubService(
        key="b", info=SimpleNamespace(progress=0.1, created_at=datetime.now())
    )
    dl = make_downloader([bad, good])

    assert dl._request_uncached(_item(), [_stream()]) is not None
    assert bad.added and good.added


def _test_no_service_accepts_still_reschedules():
    """Inside the budget, "nobody took it" means try again, not give up.

    Only the deadline is terminal -- see the all-expired test above.
    """

    bad = StubService(key="a", add_raises=RuntimeError("nope"))
    dl = make_downloader([bad])

    assert dl._request_uncached(_item(), [_stream()]) is not None


# --- get_infohash_from_url -------------------------------------------------


def _test_infohash_from_url_needs_no_request():
    """A magnet must resolve without any network call at all.

    Previously the free URL check ran only after a request that defaulted to a
    30s read with retries.
    """

    from program.services.scrapers.base import ScraperService

    def explode(*_a, **_kw):
        raise AssertionError("should not perform a request")

    session = SimpleNamespace(get=explode, close=lambda: None)

    infohash = "0326f338ab97ea05dbd6e0840d88407b99050cc4"
    got = ScraperService.get_infohash_from_url(f"magnet:?xt=urn:btih:{infohash}", session)

    assert got and got.lower() == infohash, got


def _test_infohash_from_url_empty():
    from program.services.scrapers.base import ScraperService

    assert ScraperService.get_infohash_from_url("") is None


TESTS = [
    ("progress: 0-1 fraction scale", _test_progress_fraction_scale),
    ("progress: 0-100 percent scale", _test_progress_percent_scale),
    ("progress: clamped to 0-100", _test_progress_clamped),
    ("expiry: naive recent", _test_expiry_naive_recent),
    ("expiry: naive old", _test_expiry_naive_old),
    ("expiry: timezone-aware stamps", _test_expiry_timezone_aware),
    ("expiry: garbage is not expired", _test_expiry_garbage_is_not_expired),
    ("uncached: requests fetch and reschedules", _test_requests_fetch_and_reschedules),
    ("uncached: picks best-ranked stream", _test_picks_best_ranked_stream),
    ("uncached: gives up when stalled", _test_gives_up_when_provider_stalled),
    ("uncached: old provider torrent does not expire us", _test_old_provider_torrent_does_not_expire_us),
    ("uncached: queued response is accepted", _test_queued_response_is_accepted),
    ("uncached: transient provider error retries", _test_transient_provider_error_retries_not_gives_up),
    ("uncached: gives up only when all expired", _test_gives_up_only_when_every_candidate_expired),
    ("uncached: falls through to next stream", _test_falls_through_to_next_stream),
    ("uncached: only considers top N streams", _test_only_considers_top_n_streams),
    ("uncached: wait clock stable across polls", _test_wait_clock_is_stable_across_polls),
    ("uncached: unreadable progress still waits", _test_unreadable_progress_still_waits),
    ("uncached: falls through to next service", _test_falls_through_to_next_service),
    ("uncached: no service accepts still reschedules", _test_no_service_accepts_still_reschedules),
    ("infohash: magnet needs no request", _test_infohash_from_url_needs_no_request),
    ("infohash: empty url", _test_infohash_from_url_empty),
]


def main():
    for name, fn in TESTS:
        check(name, fn)

    print(f"\nPASSED: {len(PASSED)}")
    print(f"FAILED: {len(FAILED)}")
    for name in PASSED:
        print(f"  ✓ {name}")
    for name, err in FAILED:
        print(f"  ✗ {name}: {err}")

    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
