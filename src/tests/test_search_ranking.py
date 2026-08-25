"""Search result ordering, and the Adult Empire id that kept going missing.

Stdlib-only: the modules under test import nothing beyond each other.
"""

import importlib.util
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))


def _module(name, **attrs):
    mod = types.ModuleType(name)

    for key, value in attrs.items():
        setattr(mod, key, value)

    sys.modules[name] = mod
    return mod


class _Logger:
    def __getattr__(self, _):
        return lambda *args, **kwargs: None


_module("loguru", logger=_Logger())

for pkg in ("program", "program.utils", "program.services",
            "program.services.awards", "program.services.scrapers"):
    sys.modules.setdefault(pkg, types.ModuleType(pkg))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_load("program.utils.text_matching", SRC / "program" / "utils" / "text_matching.py")
_load("program.services.awards.matching",
      SRC / "program" / "services" / "awards" / "matching.py")
ranking = _load("program.utils.search_ranking",
                SRC / "program" / "utils" / "search_ranking.py")
cat = _load("program.services.scrapers.categories",
            SRC / "program" / "services" / "scrapers" / "categories.py")


class _Record:
    def __init__(self, title, id=None):
        self.title = title
        self.id = id or title


PASSED = []
FAILED = []


def check(name, fn):
    try:
        fn()
        PASSED.append(name)
    except Exception as exc:
        FAILED.append((name, exc))


# ------------------------------------------------------------------- ranking

def test_exact_title_wins():
    """The case this exists for.

    TPDB returned these in roughly this order for "pirates", which put the
    exact match on page two where a single-page UI never saw it.
    """

    results = ranking.rank("pirates", [
        _Record("Butthole Pirates #4"),
        _Record("Interracial Butt Pirates"),
        _Record("Pirates"),
        _Record("The Sex Pirates"),
        _Record("Pirates 2: Stagnetti's Revenge"),
    ])

    assert results[0].title == "Pirates", [r.title for r in results]


def test_prefix_beats_contains():
    results = ranking.rank("pirates", [
        _Record("The Sex Pirates"),
        _Record("Pirates 2: Stagnetti's Revenge"),
    ])

    assert results[0].title == "Pirates 2: Stagnetti's Revenge"


def test_contains_beats_unrelated():
    results = ranking.rank("pirates", [
        _Record("Something Else"),
        _Record("Butthole Pirates"),
    ])

    assert results[0].title == "Butthole Pirates"


def test_ordering_is_stable_within_a_tier():
    """Equal relevance must not shuffle: the source order is the tie-break."""

    titles = ["Butthole Pirates #2", "Butthole Pirates #3", "Butthole Pirates #4"]
    results = ranking.rank("pirates", [_Record(t) for t in titles])

    assert [r.title for r in results] == titles


def test_missing_title_does_not_raise():
    results = ranking.rank("pirates", [_Record(None), _Record("Pirates")])

    assert results[0].title == "Pirates"


def test_case_is_ignored():
    results = ranking.rank("PIRATES", [_Record("Butthole Pirates"), _Record("pirates")])

    assert results[0].title == "pirates"


# ---------------------------------------------------------------- categories

def test_adult_item_searches_xxx_instead_of_movies():
    """Searching both is what buried the real "Pirates" releases.

    A one-word adult title collides with mainstream cinema constantly, and the
    mainstream categories are far larger, so the genuine matches lose.
    """

    ids = cat.select_category_ids(
        "movie", False, True, [("movie", [2000]), ("xxx", [6000])]
    )

    assert ids == {6000}, ids


def test_adult_item_falls_back_when_indexer_has_no_xxx():
    """An adult-only tracker mapped to "movie" would otherwise search nothing."""

    ids = cat.select_category_ids("movie", False, True, [("movie", [2000])])

    assert ids == {2000}, ids


def test_mainstream_item_never_gets_xxx():
    ids = cat.select_category_ids(
        "movie", False, False, [("movie", [2000]), ("xxx", [6000])]
    )

    assert ids == {2000}, ids


for _name, _fn in sorted(list(globals().items())):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")

for _name, _err in FAILED:
    print(f"  FAIL {_name}: {_err}")

sys.exit(1 if FAILED else 0)
