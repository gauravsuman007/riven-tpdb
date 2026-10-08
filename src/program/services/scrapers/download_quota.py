"""Daily .torrent download quotas, and a persistent infohash cache.

Some trackers cap how many .torrent files an account may fetch per day (PornoLab:
5, then every `dl.php` answers an HTML "limit reached" page). Prowlarr offers
a release as a download URL only, so resolving an infohash costs one of those
downloads, and a single scrape used to spend dozens. The cap is an account
rule; this module keeps within it rather than around it:

- an infohash resolved once is remembered by the release's guid, so repeat
  scrapes cost nothing;
- a capped indexer gets a rolling 24h budget, minus a reserve so the owner
  can still fetch a torrent by hand.
"""

import json
import threading
import time
from pathlib import Path

from loguru import logger

from program.utils import data_dir_path

# Lower-case substring of the Prowlarr indexer name -> .torrent files per day.
DAILY_DOWNLOAD_LIMITS: dict[str, int] = {"pornolab": 5}

# Left unspent for manual downloads from the tracker's own site.
RESERVE = 1

WINDOW_SECONDS = 24 * 3600


def daily_limit(indexer_name: str | None) -> int | None:
    name = (indexer_name or "").lower()

    for needle, limit in DAILY_DOWNLOAD_LIMITS.items():
        if needle in name:
            return limit

    return None


class DownloadLedger:
    """Infohashes by release guid, and recent download times by indexer."""

    def __init__(self, path: Path | None = None):
        self.path = path or data_dir_path / "prowlarr_downloads.json"
        self._lock = threading.Lock()
        self._hashes: dict[str, str] = {}
        self._spent: dict[str, list[float]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
            self._hashes = dict(raw.get("hashes", {}))
            self._spent = {k: list(v) for k, v in raw.get("spent", {}).items()}
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"Ignoring unreadable {self.path}: {e}")

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"hashes": self._hashes, "spent": self._spent}))
            tmp.replace(self.path)
        except Exception as e:
            logger.warning(f"Could not save {self.path}: {e}")

    def infohash(self, guid: str | None) -> str | None:
        with self._lock:
            return self._hashes.get(guid) if guid else None

    def remember(self, guid: str | None, infohash: str) -> None:
        if not guid:
            return

        with self._lock:
            self._hashes[guid] = infohash
            self._save()

    def _recent(self, key: str, now: float) -> list[float]:
        recent = [t for t in self._spent.get(key, []) if now - t < WINDOW_SECONDS]
        self._spent[key] = recent
        return recent

    def remaining(self, indexer_name: str | None, now: float | None = None) -> int | None:
        """Downloads this indexer may still be asked for, or None if uncapped."""

        limit = daily_limit(indexer_name)

        if limit is None:
            return None

        with self._lock:
            used = len(self._recent((indexer_name or "").lower(), now or time.time()))

        return max(0, limit - RESERVE - used)

    def spend(self, indexer_name: str | None, count: int = 1, now: float | None = None) -> None:
        now = now or time.time()

        with self._lock:
            self._recent((indexer_name or "").lower(), now).extend([now] * count)
            self._save()

    def exhaust(self, indexer_name: str | None, now: float | None = None) -> None:
        """The tracker refused a download: treat the day's quota as gone.

        The ledger only knows what this process spent; the account's other
        downloads (and a restart that lost the file) are invisible to it, so a
        refusal is the authoritative signal.
        """

        limit = daily_limit(indexer_name)

        if limit is None:
            return

        now = now or time.time()
        key = (indexer_name or "").lower()

        with self._lock:
            recent = self._recent(key, now)
            recent.extend([now] * max(0, limit - len(recent)))
            self._save()


ledger = DownloadLedger()
