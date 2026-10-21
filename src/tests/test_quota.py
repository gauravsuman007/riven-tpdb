"""The live download allowance PornoLab's sidecar reports as a fake search row."""

import sys
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

try:
    from program.services.scrapers import quota as q
except ImportError as exc:  # pragma: no cover
    print(f"SKIP: {exc}")
    sys.exit(0)

PASSED, FAILED = [], []
NAME = "PornoLab (no-download-quota)"


def check(name, fn):
    try:
        fn()
    except AssertionError as exc:
        FAILED.append((name, str(exc)))
    except Exception as exc:  # noqa: BLE001
        FAILED.append((name, f"{type(exc).__name__}: {exc}"))
    else:
        PASSED.append(name)


TITLE = "PornoLab quota: 48 of 50 left today | left=48 limit=50 used=2 downloaded_bytes=9 next_slot_s=0 extra=1"


def release(title, **kw):
    base = dict(title=title, seeders=None, leechers=None, grabs=None, size=None)
    base.update(kw)
    return SimpleNamespace(**base)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def row(left=48, limit=50, used=2, next_slot=0, at=1000.0, **kw):
    return q.QuotaRow(left=left, limit=limit, used=used, downloaded_bytes=0,
                      next_slot_s=next_slot, unavailable=False, unlimited=False,
                      fetched_at=at, **kw)


def tracker(**row_kw):
    clock = Clock()
    t = q.QuotaTracker(clock)
    t.record(NAME, row(**row_kw))
    return t, clock


def t_numbers_come_from_the_numeric_fields():
    r = q.parse_row(TITLE, 48, 50, 2, 777)
    assert (r.left, r.limit, r.used, r.downloaded_bytes) == (48, 50, 2, 777), r


def t_missing_seeders_means_zero_left():
    r = q.parse_row("PornoLab quota: x left=0 limit=50 used=50 next_slot_s=900", None, 50, 50, 1)
    assert r.left == 0 and r.next_slot_s == 900


def t_unavailable_and_unlimited():
    u = q.parse_row("PornoLab quota: unavailable (login) left=unknown", None, None, None, None)
    assert u.unavailable and u.left is None
    n = q.parse_row("PornoLab quota: unlimited left=-1 limit=-1", -1, -1, 0, 0)
    assert n.unlimited and n.left is None and n.limit is None


def t_row_is_split_out_of_results():
    rel, got = q.split_quota([release("Some Movie"), release(TITLE, seeders=48, leechers=50, grabs=2, size=9)], NAME)
    assert [r.title for r in rel] == ["Some Movie"] and got.left == 48


def t_another_indexers_row_is_not_ours():
    rel, got = q.split_quota([release(TITLE)], "Knaben")
    assert got is None and len(rel) == 1


def t_no_row_leaves_results_alone():
    rel, got = q.split_quota([release("a"), release("b")], NAME)
    assert got is None and len(rel) == 2


def t_go_when_left():
    t, _ = tracker()
    t.begin_grab(NAME, manual=True)


def t_stop_at_zero_and_say_when():
    t, _ = tracker(left=0, next_slot=600)
    try:
        t.begin_grab(NAME, manual=True)
    except q.QuotaStop as e:
        assert "10m" in str(e), str(e)
    else:
        raise AssertionError("went at zero")


def t_auto_keeps_one_in_reserve_but_manual_does_not():
    t, _ = tracker(left=1)
    try:
        t.begin_grab(NAME, manual=False)
    except q.QuotaStop:
        pass
    else:
        raise AssertionError("auto spent the reserve")
    t.begin_grab(NAME, manual=True)


def t_a_burst_cannot_overshoot():
    t, _ = tracker(left=2)
    t.begin_grab(NAME, manual=True)
    t.begin_grab(NAME, manual=True)
    try:
        t.begin_grab(NAME, manual=True)
    except q.QuotaStop:
        return
    raise AssertionError("third concurrent grab allowed with 2 left")


def t_next_row_resyncs_and_drops_finished_reservations():
    t, clock = tracker(left=2)
    r1 = t.begin_grab(NAME, manual=True)
    clock.now += 5
    t.end_grab(NAME, r1, ok=True)
    clock.now += 5
    t.record(NAME, row(left=1, at=clock.now))  # read after the grab finished
    assert t.left_effective(NAME) == 1
    t.begin_grab(NAME, manual=True)  # still allowed: 1 left, nothing in flight


def t_in_flight_survives_a_resync():
    t, clock = tracker(left=2)
    t.begin_grab(NAME, manual=True)
    t.record(NAME, row(left=2, at=clock.now + 1))
    assert t.left_effective(NAME) == 1


def t_unlimited_always_goes():
    t = q.QuotaTracker(Clock())
    t.record(NAME, q.QuotaRow(None, None, 0, 0, 0, False, True, 1000.0))
    for _ in range(5):
        t.begin_grab(NAME, manual=False)


def t_unavailable_allows_one_cautious_attempt():
    t = q.QuotaTracker(Clock())
    t.record(NAME, q.QuotaRow(None, None, 0, 0, 0, True, False, 1000.0))
    t.begin_grab(NAME, manual=True)
    try:
        t.begin_grab(NAME, manual=True)
    except q.QuotaStop:
        assert t.unavailable_since(NAME) == 1000.0
        return
    raise AssertionError("a second blind attempt was allowed straight away")


def t_a_failure_pauses_and_backs_off():
    t, clock = tracker(left=40)
    res = t.begin_grab(NAME, manual=True)
    t.end_grab(NAME, res, ok=False)
    try:
        t.begin_grab(NAME, manual=True)
    except q.QuotaStop as e:
        assert "paused" in str(e)
    else:
        raise AssertionError("retried straight after a failure")
    clock.now += q.COOLDOWN_FIRST + 1
    t.record(NAME, row(left=40, at=clock.now))
    res = t.begin_grab(NAME, manual=True)
    t.end_grab(NAME, res, ok=False)
    assert t._cooldown_until[NAME.lower()] - clock.now == q.COOLDOWN_FIRST * 2


def t_stale_row_is_refreshed_before_a_grab():
    t, clock = tracker(left=0, at=1000.0)
    clock.now += q.STALE_SECONDS + 1
    t.register_refresher(lambda name: row(left=9, at=clock.now))
    t.begin_grab(NAME, manual=True)
    assert t.row(NAME).left == 9


def t_refresh_is_rate_limited():
    t, clock = tracker(left=5, at=1000.0)
    calls = []
    t.register_refresher(lambda name: calls.append(1) or row(left=5, at=clock.now))
    clock.now += q.STALE_SECONDS + 1
    t._refresh(NAME)
    t._refresh(NAME)
    assert len(calls) == 1


def t_capped_by_name_before_any_row():
    t = q.QuotaTracker(Clock())
    assert t.is_capped("PornoLab") and t.is_capped(NAME) and not t.is_capped("Knaben")


for name, fn in list(globals().items()):
    if name.startswith("t_") and callable(fn):
        check(name, fn)

for name in PASSED:
    print(f"PASS {name}")
for name, why in FAILED:
    print(f"FAIL {name}: {why}")

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
sys.exit(1 if FAILED else 0)
