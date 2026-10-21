"""Candidates listed without an infohash, fetched only when chosen.

PornoLab caps .torrent downloads at 5 a day and its search results carry no
hash. A search must spend nothing; a download is spent by a manual pick or by
the automatic picker choosing such a release, and by nothing else.
"""

import hashlib
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

try:
    import bencodepy

    from program.services.scrapers import download_quota as dq
    from program.services.scrapers import pending
except ImportError as exc:  # pragma: no cover - dependency-light local runs
    print(f"SKIP: {exc}")
    sys.exit(0)

PASSED, FAILED = [], []


def check(name, fn):
    try:
        fn()
    except AssertionError as exc:
        FAILED.append((name, str(exc)))
    except Exception as exc:  # noqa: BLE001
        FAILED.append((name, f"{type(exc).__name__}: {exc}"))
    else:
        PASSED.append(name)


INFO = {b"name": b"x", b"length": 1, b"piece length": 1, b"pieces": b"0" * 20}
TORRENT = bencodepy.encode({b"announce": b"http://t", b"info": INFO})
REAL = hashlib.sha1(bencodepy.encode(INFO)).hexdigest()
FETCHES = []


def fresh(body=TORRENT, ok=True):
    dq.ledger = dq.DownloadLedger(Path(tempfile.mkdtemp()) / "l.json")
    pending.ledger = dq.ledger
    FETCHES.clear()

    def get(url, **kw):
        FETCHES.append(url)
        return SimpleNamespace(ok=ok, content=body)

    pending.requests = SimpleNamespace(get=get)
    key = dq.pending_key("g1")
    dq.ledger.register_pending(
        key, guid="g1", download_url="http://x/dl", indexer="PornoLab", privacy="private"
    )
    return key


def stream(unresolved, key="k"):
    return SimpleNamespace(
        unresolved=unresolved, infohash=key, raw_title=key, download_url=None
    )


def t_resolving_returns_the_real_hash_and_spends_one():
    key = fresh()
    assert pending.resolve_pending(key, manual=True) == REAL
    assert len(FETCHES) == 1
    assert dq.ledger.manual_remaining("PornoLab") == 4
    assert dq.ledger.infohash("g1") == REAL


def t_file_is_kept_so_the_debrid_upload_is_free():
    key = fresh()
    pending.resolve_pending(key, manual=True)
    assert dq.ledger.torrent_file("http://x/dl") == TORRENT


def t_second_resolve_costs_nothing():
    key = fresh()
    pending.resolve_pending(key, manual=True)
    pending.resolve_pending(key, manual=True)
    assert len(FETCHES) == 1


def t_refusal_empties_the_day_and_reports_it():
    key = fresh(body=b"<html>limit</html>")
    try:
        pending.resolve_pending(key, manual=True)
    except pending.PendingResolveError as e:
        assert "quota" in str(e)
    else:
        raise AssertionError("expected a refusal")
    assert dq.ledger.manual_remaining("PornoLab") == 0


def t_auto_may_not_use_the_reserve_but_manual_may():
    key = fresh()
    dq.ledger.spend("PornoLab", 4)
    try:
        pending.resolve_pending(key, manual=False)
    except pending.PendingResolveError:
        assert not FETCHES, "must not even try"
    else:
        raise AssertionError("auto used the reserve")
    assert pending.resolve_pending(key, manual=True) == REAL


def t_expired_placeholder_asks_for_a_rescrape():
    fresh()
    try:
        pending.resolve_pending("0" * 40, manual=True)
    except pending.PendingResolveError as e:
        assert "scrape" in str(e)
    else:
        raise AssertionError("expected an error")


def t_auto_fetches_only_the_pick_above_the_best_resolved():
    key = fresh()
    other = dq.pending_key("g2")
    dq.ledger.register_pending(
        other, guid="g2", download_url="http://x/2", indexer="PornoLab", privacy=None
    )
    streams = {key: stream(True, key), other: stream(True, other), "a" * 40: stream(False, "a" * 40)}
    out = pending.settle_unresolved(streams)
    assert list(out) == [REAL, "a" * 40], list(out)
    assert len(FETCHES) == 1


def t_auto_drops_placeholders_below_a_resolved_pick():
    key = fresh()
    streams = {"a" * 40: stream(False, "a" * 40), key: stream(True, key)}
    out = pending.settle_unresolved(streams)
    assert list(out) == ["a" * 40] and not FETCHES


def t_auto_drops_the_pick_when_the_budget_is_gone():
    key = fresh()
    dq.ledger.spend("PornoLab", 4)
    streams = {key: stream(True, key), "a" * 40: stream(False, "a" * 40)}
    out = pending.settle_unresolved(streams)
    assert list(out) == ["a" * 40] and not FETCHES


def t_placeholder_key_is_hash_shaped_and_stable():
    k = dq.pending_key("abc")
    assert len(k) == 40 and int(k, 16) >= 0 and k == dq.pending_key("abc")


def t_scrape_result_can_be_flagged_unresolved():
    from dataclasses import replace

    from program.services.scrapers.results import ScrapeResult

    flagged = replace(ScrapeResult(raw_title="x", seeders=3), unresolved=True)
    assert flagged.unresolved and flagged.seeders == 3
    assert not ScrapeResult(raw_title="x").unresolved


for name, fn in list(globals().items()):
    if name.startswith("t_") and callable(fn):
        check(name, fn)

for name in PASSED:
    print(f"PASS {name}")
for name, why in FAILED:
    print(f"FAIL {name}: {why}")

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
sys.exit(1 if FAILED else 0)
