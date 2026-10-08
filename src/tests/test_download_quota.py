"""PornoLab caps .torrent downloads at 5 a day; the ledger keeps scrapes inside it.

Plain script: `PYTHONPATH=src python src/tests/test_download_quota.py`.
"""

import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

try:
    from program.services.scrapers.download_quota import DownloadLedger, daily_limit
except ImportError as e:
    print(f"SKIP: {e}")
    sys.exit(0)

failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("ok   " if cond else "FAIL ") + name)
    failures += 0 if cond else 1


tmp = Path(tempfile.mkdtemp())

check("only capped indexers have a limit", daily_limit("PornoLab") == 5 and daily_limit("The Pirate Bay") is None)

ledger = DownloadLedger(tmp / "a.json")
check("a reserve is kept", ledger.remaining("PornoLab", now=1000) == 4)
ledger.spend("PornoLab", 3, now=1000)
check("spending shrinks the budget", ledger.remaining("PornoLab", now=1001) == 1)
check("the window rolls over", ledger.remaining("PornoLab", now=1000 + 24 * 3600 + 1) == 4)
check("uncapped indexers are unlimited", ledger.remaining("Knaben") is None)

ledger.exhaust("PornoLab", now=1002)
check("a refusal empties the day", ledger.remaining("PornoLab", now=1003) == 0)

first = DownloadLedger(tmp / "b.json")
first.remember("https://pornolab.net/forum/dl.php?t=1", "ab" * 20)
first.spend("PornoLab", 2)
second = DownloadLedger(tmp / "b.json")
check("hashes survive a restart", second.infohash("https://pornolab.net/forum/dl.php?t=1") == "ab" * 20)
check("spending survives a restart", second.remaining("PornoLab") == 2)

sys.exit(1 if failures else 0)
