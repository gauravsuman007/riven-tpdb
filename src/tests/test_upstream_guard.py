"""The upstream guard: per-file connection cap and 429 cooldown.

Stdlib-only, like the other self-contained suites here. Run directly:
``python3 src/tests/test_upstream_guard.py``.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "upstream_guard", SRC / "program/services/streaming/upstream_guard.py"
)
guard = importlib.util.module_from_spec(spec)
sys.modules["upstream_guard"] = guard
spec.loader.exec_module(guard)

PASSED, FAILED = [], []


def check(name, fn):
    try:
        fn()
        PASSED.append(name)
        print(f"  ok   {name}")
    except Exception as error:  # noqa: BLE001 - report, keep going
        FAILED.append((name, repr(error)))
        print(f"  FAIL {name}")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_a_seek_evicts_the_oldest_connection_not_the_newest():
    # The player that opened a new request has abandoned the old one.
    # Refusing the new one would stall exactly the seek the viewer asked for.
    async def run():
        limiter = guard.ConnectionLimiter()
        first = limiter.acquire("file.mkv", 2)
        second = limiter.acquire("file.mkv", 2)
        third = limiter.acquire("file.mkv", 2)

        assert first.cancelled.is_set()
        assert not second.cancelled.is_set()
        assert not third.cancelled.is_set()
        assert limiter.live("file.mkv") == 2

    asyncio.run(run())


def test_a_seek_storm_never_holds_more_than_the_limit():
    # Forty seeks in a row is what "too much seeking" was. Before the cap,
    # that was forty connections to the CDN for one file.
    async def run():
        limiter = guard.ConnectionLimiter()
        leases = [limiter.acquire("file.mkv", 2) for _ in range(40)]

        assert limiter.live("file.mkv") == 2
        assert sum(1 for lease in leases if not lease.cancelled.is_set()) == 2

    asyncio.run(run())


def test_other_files_are_not_evicted():
    async def run():
        limiter = guard.ConnectionLimiter()
        other = limiter.acquire("other.mkv", 1)
        limiter.acquire("file.mkv", 1)
        limiter.acquire("file.mkv", 1)

        assert not other.cancelled.is_set()

    asyncio.run(run())


def test_releasing_frees_the_slot_without_cancelling_anyone():
    async def run():
        limiter = guard.ConnectionLimiter()
        first = limiter.acquire("file.mkv", 1)
        limiter.release(first)
        second = limiter.acquire("file.mkv", 1)

        assert not first.cancelled.is_set()
        assert not second.cancelled.is_set()
        assert limiter.live("file.mkv") == 1

    asyncio.run(run())


def test_releasing_an_evicted_lease_does_not_remove_a_live_one():
    # The evicted request's finally-block runs AFTER its replacement was
    # admitted. It must not free the replacement's slot.
    async def run():
        limiter = guard.ConnectionLimiter()
        old = limiter.acquire("file.mkv", 1)
        new = limiter.acquire("file.mkv", 1)
        limiter.release(old)

        assert limiter.live("file.mkv") == 1
        assert not new.cancelled.is_set()

    asyncio.run(run())


def test_a_limit_below_one_still_admits_the_request():
    async def run():
        limiter = guard.ConnectionLimiter()
        lease = limiter.acquire("file.mkv", 0)

        assert not lease.cancelled.is_set()

    asyncio.run(run())


def test_a_429_stops_the_next_request_from_asking():
    clock = Clock()
    throttle = guard.Throttle(clock)
    throttle.refused("file.mkv")

    assert throttle.remaining("file.mkv") == guard.BASE_COOLDOWN


def test_the_cdn_retry_after_is_honoured():
    clock = Clock()
    throttle = guard.Throttle(clock)

    assert throttle.refused("file.mkv", "120") == 120


def test_a_nonsense_retry_after_falls_back_to_the_default():
    throttle = guard.Throttle(Clock())

    assert throttle.refused("file.mkv", "Wed, 21 Oct") == guard.BASE_COOLDOWN


def test_a_repeated_429_backs_off_further_and_is_capped():
    clock = Clock()
    throttle = guard.Throttle(clock)
    applied = [throttle.refused("file.mkv") for _ in range(10)]

    assert applied[:3] == [30, 60, 120]
    assert max(applied) == guard.MAX_COOLDOWN


def test_the_cooldown_expires():
    clock = Clock()
    throttle = guard.Throttle(clock)
    throttle.refused("file.mkv")
    clock.now += guard.BASE_COOLDOWN + 1

    assert throttle.remaining("file.mkv") == 0


def test_a_success_resets_the_backoff():
    clock = Clock()
    throttle = guard.Throttle(clock)
    throttle.refused("file.mkv")
    throttle.refused("file.mkv")
    throttle.succeeded("file.mkv")

    assert throttle.remaining("file.mkv") == 0
    assert throttle.refused("file.mkv") == guard.BASE_COOLDOWN


def test_a_throttled_file_does_not_throttle_another():
    throttle = guard.Throttle(Clock())
    throttle.refused("file.mkv")

    assert throttle.remaining("other.mkv") == 0


for _name, _fn in sorted(list(globals().items())):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")

for _name, _err in FAILED:
    print(f"  FAIL {_name}: {_err}")

sys.exit(1 if FAILED else 0)
