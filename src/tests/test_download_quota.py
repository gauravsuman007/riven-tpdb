"""What is remembered about .torrent files from capped trackers."""

import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

try:
    from program.services.scrapers.download_quota import DownloadLedger, pending_key
except ImportError as exc:  # pragma: no cover
    print(f"SKIP: {exc}")
    sys.exit(0)

failures = []


def check(name, cond):
    if not cond:
        failures.append(name)
        print(f"FAIL {name}")


tmp = Path(tempfile.mkdtemp())
url = "https://pornolab.net/forum/dl.php?t=1"

first = DownloadLedger(tmp / "a.json")
first.remember(url, "ab" * 20)
second = DownloadLedger(tmp / "a.json")
check("hashes survive a restart", second.infohash(url) == "ab" * 20)
check("unknown guid is unknown", second.infohash("nope") is None)
check("no guid is unknown", second.infohash(None) is None)

second.keep_torrent_file("http://x/dl", b"d4:infod0:ee")
check("a kept torrent file reads back", DownloadLedger(tmp / "a.json").torrent_file("http://x/dl") == b"d4:infod0:ee")
check("an unseen url has no file", second.torrent_file("http://x/other") is None)

second.register_pending("k", guid="g", download_url="u", indexer="PornoLab")
check("pending is remembered", second.pending("k")["guid"] == "g")
check("pending is not persisted", DownloadLedger(tmp / "a.json").pending("k") is None)

# An older file carried a hand-kept count; it must be ignored, not crash.
(tmp / "old.json").write_text('{"hashes": {"g": "cd"}, "spent": {"pornolab": [1, 2]}}')
check("an old file with a spent count still loads", DownloadLedger(tmp / "old.json").infohash("g") == "cd")

check("placeholder is stable and hash-shaped", len(pending_key("a")) == 40 and pending_key("a") == pending_key("a"))

sys.exit(1 if failures else 0)
