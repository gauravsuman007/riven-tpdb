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
