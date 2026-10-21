"""Reconciling the saved Prowlarr indexer selection with what Prowlarr has.

The case that prompted it: the saved selection was [1, 25, 23, 15, 9, 10, 14]
while Prowlarr listed 1, 4, 5, 9, 19, 20, 22 and 25 -- four ids matched nothing
(Prowlarr had been rebuilt, PornoLab 23 -> 25) and nothing newer was searched.
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

try:
    from program.services.scrapers import prowlarr as mod
    from program.settings.models import ProwlarrConfig
except ImportError as exc:  # pragma: no cover - dependency-light local runs
    print(f"SKIP: {exc}")
    sys.exit(0)

PASSED = []
FAILED = []
SAVES = []


class _Manager:
    def save(self):
        SAVES.append(1)


mod.settings_manager = _Manager()


def check(name, fn):
    SAVES.clear()
    try:
        fn()
    except AssertionError as exc:
        FAILED.append((name, str(exc)))
    except Exception as exc:  # noqa: BLE001
        FAILED.append((name, f"{type(exc).__name__}: {exc}"))
    else:
        PASSED.append(name)


def cfg(selected, known):
    return ProwlarrConfig(indexer_ids=selected, known_indexer_ids=known)


LIVE = {1, 4, 5, 9, 19, 20, 22, 25}


def t_dead_ids_are_dropped():
    c = cfg([1, 25, 23, 15, 9, 10, 14], sorted(LIVE))
    assert mod.sync_indexer_selection(c, LIVE)
    assert c.indexer_ids == [1, 25, 9], c.indexer_ids
    assert SAVES


def t_new_indexer_is_added():
    c = cfg([1, 25], [1, 25, 9])
    mod.sync_indexer_selection(c, {1, 25, 9, 30})
    assert c.indexer_ids == [1, 25, 30], c.indexer_ids
    assert c.known_indexer_ids == [1, 9, 25, 30]


def t_unticked_indexer_stays_unticked():
    c = cfg([1, 25], [1, 9, 25])  # 9 is known and not selected: a choice
    mod.sync_indexer_selection(c, {1, 9, 25})
    assert c.indexer_ids == [1, 25], c.indexer_ids


def t_first_run_adopts_without_adding():
    c = cfg([1, 25, 23], [])
    mod.sync_indexer_selection(c, LIVE)
    assert c.indexer_ids == [1, 25], c.indexer_ids
    assert c.known_indexer_ids == sorted(LIVE)


def t_empty_selection_stays_empty():
    c = cfg([], [1, 9])
    mod.sync_indexer_selection(c, {1, 9, 30})
    assert c.indexer_ids == [], c.indexer_ids
    assert c.known_indexer_ids == [1, 9, 30]


def t_empty_answer_changes_nothing():
    c = cfg([1, 25], [1, 25])
    assert not mod.sync_indexer_selection(c, set())
    assert c.indexer_ids == [1, 25] and not SAVES


def t_in_sync_writes_nothing():
    c = cfg([1, 25], [1, 9, 25])
    assert not mod.sync_indexer_selection(c, {1, 9, 25})
    assert not SAVES


for name, fn in list(globals().items()):
    if name.startswith("t_") and callable(fn):
        check(name, fn)

for name in PASSED:
    print(f"PASS {name}")
for name, why in FAILED:
    print(f"FAIL {name}: {why}")

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
sys.exit(1 if FAILED else 0)
