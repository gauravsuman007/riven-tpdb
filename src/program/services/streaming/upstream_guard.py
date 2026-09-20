"""How many times, and how often, this server may ask a debrid CDN for one file.

WHY THIS EXISTS
---------------
An external player seeks by opening a NEW range request, and it abandons the
old one rather than closing it cleanly -- ExoPlayer, MX and VLC all do this,
and VLC also opens a second connection to read ``moov`` off the tail of a
non-faststart file. Proxied through ``/stream/file``, every one of those
became its own connection from this server to the CDN.

TorBox's CDN limits connections and request rate per file (their own advice
is one connection). Enough seeking and it answers **429 Too Many Requests**,
and it keeps answering it: measured 2026-09-13/14, the file stayed refused for
about forty minutes, to a single request with nothing else open, and to a
freshly minted link for the same file -- while other files on the same
account served 206.

Two things made that worse, and both are fixed alongside this module:

* ``/stream/file`` treated every status below 500 as "this link is spent" and
  re-minted it. 429 is below 500. So a throttled seek cost a TorBox API call
  AND another CDN request, on a file already being refused.
* ``playback_url.verify`` treated 429 as dead, so the HLS and remux paths --
  which verify on every playlist and every segment -- re-minted too.

WHAT IT DOES
------------
``ConnectionLimiter`` caps live upstream connections per file. At the cap
the OLDEST is closed rather than the newest refused, because a player that
opened a new request after seeking has already abandoned the old one; queueing
the new request behind a connection nobody is reading would stall the seek.

``RateLimiter`` spaces out NEW upstream requests for one file. This is the
half that was missing, and it is why the 429 came back after the connection
cap shipped: seeking is a RATE problem, not a concurrency one. An external
player that seeks forward eight times opens eight requests one after another,
each one abandoning the last, so at no moment are more than one or two live --
the cap never trips -- while the CDN sees eight requests for one file in a few
seconds and refuses the ninth. Measured on item 869 (2026-09-19): the
connection cap was in force the whole time and the file was still throttled.

Waiting is deliberate rather than refusing. A seek that arrives half a second
late is a seek; a seek that returns 503 is a stopped video, and a player that
gets one usually retries immediately, which is what earned the throttle.

``Throttle`` remembers a 429 per file and answers 503 with ``Retry-After``
until it passes, instead of asking again. Asking a CDN that is refusing you is
how a short throttle becomes a long one. Honours the CDN's own
``Retry-After``; otherwise starts at 30s and doubles on each repeat, to ten
minutes, and clears on the first success.

Pure asyncio and stdlib, so it is testable without the framework.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field

#: New upstream requests allowed for one file in a burst, before spacing in.
BURST = 6
#: Sustained rate afterwards, in new requests per second, per file.
REFILL_PER_SECOND = 0.5
#: Longer than this and waiting is worse than saying so; 503 instead.
MAX_WAIT = 10.0

#: First cooldown after a 429 with no Retry-After, in seconds.
BASE_COOLDOWN = 30.0
#: The longest a repeated 429 is allowed to push the cooldown.
MAX_COOLDOWN = 600.0


@dataclass
class Lease:
    """One live upstream connection. ``cancelled`` is set when it is evicted."""

    key: str
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)


class ConnectionLimiter:
    """At most ``limit`` live upstream connections per key; the newest wins."""

    def __init__(self) -> None:
        self._live: dict[str, OrderedDict[int, Lease]] = {}

    def acquire(self, key: str, limit: int) -> Lease:
        leases = self._live.setdefault(key, OrderedDict())

        while len(leases) >= max(1, limit):
            _, oldest = leases.popitem(last=False)
            oldest.cancelled.set()

        lease = Lease(key=key)
        leases[id(lease)] = lease

        return lease

    def release(self, lease: Lease) -> None:
        leases = self._live.get(lease.key)

        if leases is None:
            return

        leases.pop(id(lease), None)

        if not leases:
            del self._live[lease.key]

    def live(self, key: str) -> int:
        return len(self._live.get(key, ()))


class RateLimiter:
    """A token bucket per file: burst freely, then settle to a steady rate.

    Normal playback is unaffected. A file played start to finish is ONE
    upstream request that streams for an hour, so it spends a single token;
    the bucket only empties when a player is opening request after request,
    which is exactly the behaviour that gets a file refused.
    """

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._tokens: dict[str, float] = {}
        self._checked: dict[str, float] = {}

    def _refill(self, key: str) -> float:
        now = self._clock()
        last = self._checked.get(key, now)
        tokens = self._tokens.get(key, float(BURST))

        tokens = min(float(BURST), tokens + (now - last) * REFILL_PER_SECOND)

        self._checked[key] = now
        self._tokens[key] = tokens

        return tokens

    def delay(self, key: str) -> float:
        """Seconds to wait before asking for this file. Spends a token."""

        tokens = self._refill(key)

        if tokens >= 1.0:
            self._tokens[key] = tokens - 1.0
            return 0.0

        # Go into debt rather than round up: several waiters queue behind each
        # other instead of all waking at the same instant and bursting again.
        self._tokens[key] = tokens - 1.0

        return (1.0 - tokens) / REFILL_PER_SECOND

    async def reserve(self, key: str) -> None:
        """Wait our turn. Raises ``TooBusy`` when the wait is unreasonable."""

        wait = self.delay(key)

        if wait > MAX_WAIT:
            # Put the token back: we are not making this request after all,
            # and charging for a refusal would push the queue out further.
            self._tokens[key] = self._tokens.get(key, 0.0) + 1.0
            raise TooBusy(wait)

        if wait > 0:
            await asyncio.sleep(wait)

    def forget(self, key: str) -> None:
        self._tokens.pop(key, None)
        self._checked.pop(key, None)


class TooBusy(Exception):
    """Too many requests are already queued for this file to join them."""

    def __init__(self, wait: float) -> None:
        super().__init__(f"{wait:.1f}s of requests are already queued")
        self.wait = wait


class Throttle:
    """Per-key memory of a 429, so the next request does not repeat it."""

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._until: dict[str, float] = {}
        self._last: dict[str, float] = {}

    def refused(self, key: str, retry_after: str | None = None) -> float:
        """Record a 429. Returns the cooldown applied, in seconds."""

        seconds: float | None = None

        if retry_after:
            try:
                seconds = max(1.0, float(retry_after))
            except ValueError:
                seconds = None

        if seconds is None:
            previous = self._last.get(key)
            seconds = BASE_COOLDOWN if previous is None else previous * 2

        seconds = min(seconds, MAX_COOLDOWN)
        self._last[key] = seconds
        self._until[key] = self._clock() + seconds

        return seconds

    def remaining(self, key: str) -> float:
        """Seconds left on this key's cooldown; 0 when it may be asked."""

        until = self._until.get(key)

        if until is None:
            return 0.0

        left = until - self._clock()

        if left <= 0:
            del self._until[key]
            return 0.0

        return left

    def succeeded(self, key: str) -> None:
        self._until.pop(key, None)
        self._last.pop(key, None)


limiter = ConnectionLimiter()
throttle = Throttle()
rate = RateLimiter()
