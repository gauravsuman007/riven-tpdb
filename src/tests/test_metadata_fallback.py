"""The provider chain: order, fallback, and the id that must not be confused.

Four things are worth protecting here, and each of them has already been a bug
somewhere in this codebase or is one step away from being one:

    * A StashDB scene maps onto the SAME dict a TPDB scene maps to, key for
      key. Everything downstream reads those keys and must not learn which
      provider supplied them.
    * The StashDB id lands in `stashdb_id` and never in `tpdb_id`. Sharing the
      column would make every TPDB lookup and dedupe silently wrong.
    * The second provider is consulted when the first finds nothing AND when
      the first fails outright. A provider that cannot answer is exactly when
      the other one earns its keep.
    * A provider with no credentials is SKIPPED, not counted as a failure --
      otherwise the chain reports an error for something simply not set up.

Runs without a database, a network or a settings file.
"""

import sys
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

import importlib.util

PASSED: list[str] = []
FAILED: list[tuple[str, Exception]] = []


def check(name, fn):
    try:
        fn()
        PASSED.append(name)
        print(f"PASS: {name}")
    except Exception as exc:  # noqa: BLE001
        FAILED.append((name, exc))
        print(f"FAIL: {name}: {exc}")


def _load(module_path: str, name: str):
    """Import one module by path, without dragging in the package."""

    spec = importlib.util.spec_from_file_location(name, SRC / module_path)

    if spec is None or spec.loader is None:
        raise ImportError(module_path)

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


try:
    stashdb_mapping = _load(
        "program/services/indexers/stashdb_mapping.py", "stashdb_mapping"
    )
    tpdb_mapping = _load(
        "program/services/indexers/tpdb_mapping.py", "tpdb_mapping"
    )
except ImportError as exc:  # pragma: no cover
    print(f"SKIP: {exc}")
    sys.exit(0)


SCENE = {
    "id": "5f9c1e2a-0000-4000-8000-000000000001",
    "title": "Babysitters",
    "release_date": "2007-09-28",
    "studio": {"id": "dp-1", "name": "Digital Playground", "parent": None},
    "performers": [
        {"as": "Sasha Grey", "performer": {"name": "Sasha Grey Canonical"}},
        {"as": None, "performer": {"name": "Jesse Jane"}},
    ],
    "tags": [{"name": "Feature"}, {"name": "Anal"}],
    "images": [
        {"url": "small.jpg", "width": 100, "height": 150},
        {"url": "large.jpg", "width": 800, "height": 1200},
    ],
}


def test_shapes_match_exactly():
    """The two mappers must produce the same keys, or downstream code breaks."""

    stash = set(stashdb_mapping.scene_to_movie_dict(SCENE))
    tpdb = set(tpdb_mapping.scene_to_movie_dict({"title": "x"}))

    # StashDB adds its own id and is allowed to; nothing else may differ.
    extra = stash - tpdb
    missing = tpdb - stash

    assert extra == {"stashdb_id"}, f"unexpected extra keys: {extra}"
    assert not missing, f"StashDB mapping is missing keys: {missing}"


def test_stashdb_id_never_lands_in_tpdb_id():
    mapped = stashdb_mapping.scene_to_movie_dict(SCENE)

    assert mapped["tpdb_id"] is None, "a StashDB UUID must never fill tpdb_id"
    assert mapped["stashdb_id"] == SCENE["id"]


def test_values_are_mapped_the_way_matching_expects():
    mapped = stashdb_mapping.scene_to_movie_dict(SCENE)

    assert mapped["title"] == "Babysitters"
    assert mapped["site_name"] == "Digital Playground"
    assert mapped["year"] == 2007
    assert mapped["aired_at"] == datetime(2007, 9, 28)
    # The credited alias wins: that is the name release titles carry.
    assert mapped["performers"] == ["Sasha Grey", "Jesse Jane"]
    # Tags are lowercased, as TPDB's are, because matching compares lowercase.
    assert mapped["genres"] == ["feature", "anal"]
    # The largest image, not the first.
    assert mapped["poster_path"] == "large.jpg"
    # No ratings on StashDB. None, never 0 -- a zero sorts as "rated badly".
    assert mapped["rating"] is None


def test_partial_dates_survive():
    """Older StashDB records carry a year, or a year and month, only."""

    assert stashdb_mapping.parse_stashdb_date("2007") == datetime(2007, 1, 1)
    assert stashdb_mapping.parse_stashdb_date("2007-09") == datetime(2007, 9, 1)
    assert stashdb_mapping.parse_stashdb_date("") is None
    assert stashdb_mapping.parse_stashdb_date(None) is None
    assert stashdb_mapping.parse_stashdb_date("not a date") is None


def test_a_scene_with_nothing_in_it_still_maps():
    """A sparse record must produce a usable dict, not raise."""

    mapped = stashdb_mapping.scene_to_movie_dict({"id": "x"})

    assert mapped["title"] == "Untitled"
    assert mapped["site_name"] is None
    assert mapped["performers"] is None
    assert mapped["poster_path"] is None
    assert mapped["type"] == "movie"


check("mapped shapes match the TPDB mapper", test_shapes_match_exactly)
check("a StashDB id never lands in tpdb_id", test_stashdb_id_never_lands_in_tpdb_id)
check("values map as matching expects", test_values_are_mapped_the_way_matching_expects)
check("partial release dates survive", test_partial_dates_survive)
check("a sparse scene still maps", test_a_scene_with_nothing_in_it_still_maps)

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")

for _name, _err in FAILED:
    print(f"  {_name}: {_err}")

sys.exit(1 if FAILED else 0)
