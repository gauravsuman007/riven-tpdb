"""What the scrapers remember about .torrent files from capped trackers.

Some trackers cap how many .torrent files an account may download per day
(PornoLab). Their search results carry no infohash, so a release is listed under
a placeholder and its file is fetched only when it is chosen (`pending.py`).
How many downloads are LEFT is not tracked here: it is read live from the
tracker's own quota row (`quota.py`).

This module keeps what is worth not paying for twice:

- an infohash resolved once is remembered by the release's guid (persisted), so
  a repeat scrape lists the release as an ordinary one;
- the downloaded .torrent itself is kept, so uploading it to a debrid service
  later does not need a second download;
- placeholders awaiting a choice (in memory: they hold a download URL, and that
  carries the Prowlarr API key).
"""

import hashlib
import json
import threading
from pathlib import Path

from loguru import logger

from program.utils import data_dir_path


def pending_key(guid: str) -> str:
    """Stable placeholder for a release whose real infohash is not known.

    40 hex characters because everything downstream validates an infohash by
    shape. It is a hash of the guid, not of any torrent, so it must never reach
    a debrid service -- `queue_release` swaps it for the real one first.
    """

    return hashlib.sha1(f"pending:{guid}".encode()).hexdigest()


class DownloadLedger:
    """Infohashes by release guid, kept torrent files, and pending candidates."""

    def __init__(self, path: Path | None = None):
        self.path = path or data_dir_path / "prowlarr_downloads.json"
        self._lock = threading.Lock()
        self._hashes: dict[str, str] = {}
        self._pending: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
            # An older file also held a hand-kept count of downloads spent;
            # that is read live from the tracker now, so it is ignored.
            self._hashes = dict(raw.get("hashes", {}))
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"Ignoring unreadable {self.path}: {e}")

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"hashes": self._hashes}))
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

    def register_pending(self, key: str, **details) -> None:
        with self._lock:
            self._pending.pop(key, None)
            self._pending[key] = details

            while len(self._pending) > 2000:
                self._pending.pop(next(iter(self._pending)))

    def pending(self, key: str) -> dict | None:
        with self._lock:
            return self._pending.get(key)

    def torrent_file(self, url: str) -> bytes | None:
        """A .torrent already downloaded for this URL, if we kept it."""

        try:
            return self._torrent_path(url).read_bytes()
        except OSError:
            return None

    def keep_torrent_file(self, url: str, content: bytes) -> None:
        """Keep a downloaded .torrent so the debrid upload does not need a
        second download, which on a capped tracker is a second slot spent."""

        try:
            path = self._torrent_path(url)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        except OSError as e:
            logger.warning(f"Could not keep the torrent file: {e}")

    def _torrent_path(self, url: str) -> Path:
        return (
            self.path.parent
            / "torrent_files"
            / (hashlib.sha1(url.encode()).hexdigest() + ".torrent")
        )


ledger = DownloadLedger()
