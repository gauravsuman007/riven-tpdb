"""Phase 4 tests: adult torrent-indexer category handling (Prowlarr/Jackett).

Verifies the pure category helpers that make adult (Newznab 6000 "XXX")
trackers first-class, so TPDB items can be searched on adult indexers.
"""

import importlib.util
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))


def _load_real(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cat = _load_real(
    "tpdb_categories_real", SRC / "program" / "services" / "scrapers" / "categories.py"
)

PASSED = []
FAILED = []


def check(name, fn):
    try:
        fn()
    except AssertionError as exc:
        FAILED.append((name, str(exc)))
    except Exception as exc:  # noqa: BLE001
        FAILED.append((name, f"{type(exc).__name__}: {exc}"))
    else:
        PASSED.append(name)


def _test_is_adult_category():
    for name in ["XXX", "xxx", "Adult", "adult", "Porn", "porn", "18+", "XXX - Adult"]:
        assert cat.is_adult_category(name), f"{name!r} should be adult"
    for name in ["Movies", "TV", "Anime", "TV/SD", "", None]:
        assert not cat.is_adult_category(name), f"{name!r} should NOT be adult"


def _test_select_adult_only_indexer():
    # Adult-only tracker: only an XXX category.
    ids = cat.select_category_ids("movie", False, True, [("xxx", [6000, 6010])])
    assert ids == {6000, 6010}


def _test_select_adult_on_general_indexer():
    # General tracker with both Movies and XXX. An adult item searches XXX
    # *instead of* Movies: searching both buried the real matches for "Pirates"
    # under five Pirates of the Caribbean films and a release group of the same
    # name, because a one-word adult title collides with mainstream cinema and
    # the mainstream categories are far larger.
    ids = cat.select_category_ids(
        "movie", False, True, [("movie", [2000]), ("xxx", [6000])]
    )
    assert ids == {6000}, ids


def _test_select_adult_falls_back_without_xxx():
    # An adult-only tracker whose categories Prowlarr maps to "movie" exposes
    # no XXX category. Restricting it to one would search nothing at all.
    ids = cat.select_category_ids("movie", False, True, [("movie", [2000])])
    assert ids == {2000}, ids


def _test_select_non_adult_movie_excludes_xxx():
    ids = cat.select_category_ids(
        "movie", False, False, [("movie", [2000]), ("xxx", [6000])]
    )
    assert ids == {2000}


def _test_select_anime():
    ids = cat.select_category_ids(
        "movie", True, False, [("movie", [2000]), ("anime", [5070])]
    )
    assert ids == {2000, 5070}


TESTS = [
    ("categories: is_adult_category detection", _test_is_adult_category),
    ("categories: adult-only indexer ids", _test_select_adult_only_indexer),
    ("categories: adult on general indexer", _test_select_adult_on_general_indexer),
    ("categories: adult falls back without xxx", _test_select_adult_falls_back_without_xxx),
    ("categories: non-adult movie excludes xxx", _test_select_non_adult_movie_excludes_xxx),
    ("categories: anime category included", _test_select_anime),
]


def main():
    for name, fn in TESTS:
        check(name, fn)

    print(f"\nPASSED: {len(PASSED)}")
    print(f"FAILED: {len(FAILED)}")
    for name in PASSED:
        print(f"  \u2713 {name}")
    for name, err in FAILED:
        print(f"  \u2717 {name}: {err}")


if __name__ == "__main__":
    main()