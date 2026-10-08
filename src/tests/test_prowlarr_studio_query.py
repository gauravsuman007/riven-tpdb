"""The studio-qualified second search Prowlarr runs for adult movies.

"Drive" (Deeper, 2019) is the case this exists for: searched bare, the title
drew 482 unrelated releases and every indexer's result cap cut the film's own
1080p and 720p packs off the end. "Drive Deeper" finds them first.
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

try:
    from program.media.item import Movie, Show
    from program.services.scrapers.prowlarr import studio_query
except ImportError as exc:  # pragma: no cover - dependency-light local runs
    print(f"SKIP: {exc}")
    sys.exit(0)

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


def movie(title="Drive", site_name="Deeper", **ids):
    item = Movie({"title": title, "type": "movie"})
    item.site_name = site_name
    item.tpdb_id = ids.get("tpdb_id", "a66c538b-8a2e-48d7-bf99-d7619da3d716")
    item.adultempire_id = ids.get("adultempire_id")
    return item


def t_adds_studio():
    assert studio_query(movie()) == "Drive Deeper"


def t_compound_studio_uses_first_part():
    assert studio_query(movie(site_name="Deeper/Pulse")) == "Drive Deeper"


def t_adult_empire_only_title_counts():
    item = movie(tpdb_id=None, adultempire_id="2717528")
    assert studio_query(item) == "Drive Deeper"


def t_no_studio_no_second_search():
    assert studio_query(movie(site_name=None)) is None
    assert studio_query(movie(site_name="  ")) is None


def t_studio_already_in_title_is_not_repeated():
    assert studio_query(movie(title="Deeper Love", site_name="Deeper")) is None


def t_mainstream_movie_is_left_alone():
    assert studio_query(movie(tpdb_id=None)) is None


def t_shows_are_left_alone():
    show = Show({"title": "Drive", "type": "show"})
    show.site_name = "Deeper"
    show.tpdb_id = "x"
    assert studio_query(show) is None


for name, fn in list(globals().items()):
    if name.startswith("t_") and callable(fn):
        check(name, fn)

for name in PASSED:
    print(f"PASS {name}")
for name, why in FAILED:
    print(f"FAIL {name}: {why}")

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
sys.exit(1 if FAILED else 0)
