"""Housekeeping for a TorBox account, decided from its torrent list.

TorBox caps ACTIVE downloads (3 on this plan), and every torrent that is not
finished holds one of those slots for as long as it sits there. Dead torrents
therefore starve new ones: a magnet whose metadata never resolved, or a swarm
with no seeds, occupies a slot indefinitely and nothing newer can start.

The rules, in order of how final they are:

* metadata still unresolved after ``UNRESOLVED_AFTER``  -> DELETE. Nothing was
  ever downloaded, so nothing is lost.
* stalled for ``STOP_AFTER``                             -> STOP. Frees the slot
  and keeps every file and the progress made so far.
* stalled for ``PAUSE_AFTER`` and a new download needs a slot
                                                         -> STOP, remembered, and
  RESUMED once there is room again.

"Stop" is TorBox's ``stop_seeding``: the API has no pause, and that operation
is what takes an unfinished torrent out of the active count without touching
its files (checked against a 265 GB torrent at 86%: size and progress
unchanged, ``active`` false).

TorBox does not say how long a torrent has been stalled -- ``updated_at`` never
moves -- so first sightings are recorded here and measured from then on.

This module only DECIDES. The calls to TorBox live in ``torbox.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

UNRESOLVED_AFTER = timedelta(minutes=5)
PAUSE_AFTER = timedelta(hours=5)
STOP_AFTER = timedelta(hours=48)

# A resume TorBox refuses this many times is not going to work, and retrying it
# on every pass would only log the same failure for ever.
MAX_RESUME_ATTEMPTS = 3


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(stamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def is_stalled(torrent: dict[str, Any]) -> bool:
    """An unfinished torrent that is active and going nowhere."""

    return bool(
        torrent.get("active")
        and not torrent.get("download_finished")
        and str(torrent.get("download_state") or "").lower().startswith("stalled")
    )


def is_unresolved(torrent: dict[str, Any], now: datetime) -> bool:
    """Metadata never arrived, and it has had long enough.

    A magnet with no metadata is listed under its own hash as its name (or sits
    in ``checking``) with no progress. Anything that has made progress, or is
    finished, has metadata by definition and is never matched here.
    """

    if torrent.get("download_finished") or (torrent.get("progress") or 0) > 0:
        return False

    if torrent.get("owner") != torrent.get("auth_id"):
        # Not ours to delete.
        return False

    name = str(torrent.get("name") or "").lower()
    state = str(torrent.get("download_state") or "").lower()
    nameless = name == str(torrent.get("hash") or "").lower()

    if not (nameless or state.startswith(("checking", "metadl", "fetching"))):
        return False

    created = _parse(str(torrent.get("created_at") or ""))

    return created is not None and now - created > UNRESOLVED_AFTER


@dataclass
class State:
    """What survives a restart: when each torrent was first seen stalled, and
    which ones we paused to make room (so they can be brought back)."""

    path: Path | None = None
    stalled_since: dict[str, str] = field(default_factory=dict)
    paused: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "State":
        try:
            raw = json.loads(path.read_text())

            return cls(
                path=path,
                stalled_since=dict(raw.get("stalled_since") or {}),
                paused=dict(raw.get("paused") or {}),
            )
        except (OSError, ValueError, AttributeError):
            return cls(path=path)

    def save(self) -> None:
        if self.path is None:
            return

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {"stalled_since": self.stalled_since, "paused": self.paused}
                )
            )
            tmp.replace(self.path)
        except OSError:
            pass

    def stalled_for(self, torrent: dict[str, Any], now: datetime) -> timedelta:
        since = _parse(self.stalled_since.get(str(torrent.get("hash") or "").lower(), ""))

        return now - since if since else timedelta(0)

    def observe(self, torrents: list[dict[str, Any]], now: datetime) -> None:
        """Record first sightings of stalled torrents; forget the recovered."""

        stalled = {
            str(t.get("hash") or "").lower() for t in torrents if is_stalled(t)
        }

        for key in stalled:
            self.stalled_since.setdefault(key, now.isoformat())

        for key in [k for k in self.stalled_since if k not in stalled]:
            del self.stalled_since[key]


@dataclass
class Plan:
    delete: list[dict[str, Any]] = field(default_factory=list)
    stop: list[dict[str, Any]] = field(default_factory=list)


def plan(torrents: list[dict[str, Any]], state: State, now: datetime) -> Plan:
    """What to remove and what to stop, with no new download in the picture."""

    result = Plan()

    for torrent in torrents:
        if is_unresolved(torrent, now):
            result.delete.append(torrent)
        elif is_stalled(torrent) and state.stalled_for(torrent, now) >= STOP_AFTER:
            result.stop.append(torrent)

    return result


def pause_candidates(
    torrents: list[dict[str, Any]], state: State, now: datetime
) -> list[dict[str, Any]]:
    """Stalled long enough to give up a slot, longest-stalled first."""

    ready = [
        t
        for t in torrents
        if is_stalled(t) and state.stalled_for(t, now) >= PAUSE_AFTER
    ]

    return sorted(ready, key=lambda t: state.stalled_for(t, now), reverse=True)


def in_flight(torrents: list[dict[str, Any]]) -> bool:
    """Is a download genuinely making its way, so slots should be left alone?

    Used to decide when it is safe to bring paused torrents back: while a new
    download is still running, resuming one could take the slot it needs.
    """

    return any(
        t.get("active")
        and not t.get("download_finished")
        and not is_stalled(t)
        and str(t.get("download_state") or "").lower().startswith(
            ("downloading", "metadl", "checking", "fetching")
        )
        for t in torrents
    )
