"""The recommendation engine: facets, intents and ranking.

Stdlib-only and self-contained, like the other suites here: the modules under
test are loaded by path and their framework dependencies stubbed, so nothing
touches a network, a database or FUSE. That is possible at all because of how
the engine is split -- the movie engine is arithmetic over rows that were
already fetched, and the intent layer is a pure expression.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))


def _stub(name, **attrs):
    if name in sys.modules:
        return sys.modules[name]

    module = types.ModuleType(name)

    for key, value in attrs.items():
        setattr(module, key, value)

    sys.modules[name] = module

    return module


class _Anything:
    """Stands in for anything the modules import but these tests never call."""

    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, _):
        return _Anything()

    def __call__(self, *args, **kwargs):
        return _Anything()


class _Logger:
    def __getattr__(self, _):
        return lambda *args, **kwargs: None


_stub("loguru", logger=_Logger())
_stub("requests", Session=_Anything, RequestException=Exception)

_settings = SimpleNamespace(
    stashdb=SimpleNamespace(cache_dir=Path("/nonexistent/stashdb-cache"))
)
_stub("program.settings", settings_manager=SimpleNamespace(settings=_settings))
_stub("program.utils", data_dir_path=Path("/nonexistent/data"))
_stub("program.utils.time", utcnow=__import__("datetime").datetime.now)
_stub("program.db", db_session=_Anything)
_stub("program.db.db", db_session=_Anything)
_stub("program.media", MediaItem=_Anything)
_stub("program.media.item", MediaItem=_Anything())
_stub("program.media.collection", Collection=_Anything(), CollectionEntry=_Anything())
_stub(
    "sqlalchemy",
    select=lambda *a, **k: _Anything(),
    func=_Anything(),
)
_stub("sqlalchemy.orm", joinedload=lambda *a, **k: _Anything())
_stub(
    "program.apis.stashdb_api",
    StashdbApi=_Anything,
    StashdbApiError=type("StashdbApiError", (Exception,), {}),
)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)

    return module


RECS = SRC / "program" / "services" / "recommendations"
facets_module = _load("program.services.recommendations.facets", RECS / "facets.py")
intents_module = _load("program.services.recommendations.intents", RECS / "intents.py")
engine_module = _load("program.services.recommendations.engine", RECS / "engine.py")

Facet = facets_module.Facet
FacetVocabulary = facets_module.FacetVocabulary
TagGraph = facets_module.TagGraph
UNKNOWN = facets_module.UNKNOWN
parse_key = facets_module.parse_key

Intent = intents_module.Intent
IntentLibrary = intents_module.IntentLibrary
DEFAULT_INTENTS = intents_module.DEFAULT_INTENTS

MovieEngine = engine_module.MovieEngine
SceneEngine = engine_module.SceneEngine
LibraryTaste = engine_module.LibraryTaste
AWARD_WEIGHTS = engine_module.AWARD_WEIGHTS
NOMINEE_FRACTION = engine_module.NOMINEE_FRACTION

PASSED = []
FAILED = []


def check(name, fn):
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - the harness reports, it does not raise
        FAILED.append((name, e))
        print(f"  FAIL {name}")
    else:
        PASSED.append(name)
        print(f"  ok   {name}")


def _empty_vocabulary():
    vocabulary = FacetVocabulary(api=SimpleNamespace(configured=False))
    vocabulary._graph = TagGraph(by_value={}, ids={})

    return vocabulary


# --------------------------------------------------------------- the facets


def test_unknown_values_are_kept_but_not_categorised():
    """An unrecognised tag must not be given a plausible category.

    The failure this guards is silent: a value guessed into ``Moods`` would be
    matched by every mood intent forever, with nothing to show it was invented.
    """

    facet = _empty_vocabulary().facet("brown hair")

    assert facet.category == UNKNOWN, facet
    assert facet.value == "brown hair", facet


def test_aliases_restore_the_kind_of_a_flat_genre():
    vocabulary = _empty_vocabulary()

    assert vocabulary.facet("narrative").category == "Themes"
    assert vocabulary.facet("Outdoors").category == "Locations"


def test_the_ingested_graph_wins_over_the_alias_table():
    """StashDB is the canonical vocabulary; the aliases are only the fallback."""

    vocabulary = _empty_vocabulary()
    vocabulary._graph = TagGraph(
        by_value={"romance": Facet("SCENE", "Moods", "Romance")},
        ids={"romance": "uuid-1"},
    )

    assert vocabulary.facet("Romance").value == "Romance"
    assert vocabulary.tag_id("Moods:Romance") == "uuid-1"
    assert vocabulary.tag_ids(["Moods:Romance", "Themes:Nonsense"]) == ["uuid-1"]


def test_absorb_indexes_every_alias_stashdb_records():
    by_value, ids = {}, {}

    FacetVocabulary._absorb(
        {
            "id": "uuid-2",
            "name": "Outdoors",
            "aliases": ["Outside"],
            "category": {"name": "Locations", "group": "SCENE"},
        },
        by_value,
        ids,
    )

    assert ids == {"outdoors": "uuid-2", "outside": "uuid-2"}, ids
    assert by_value["outside"].category == "Locations"


def test_parse_key_handles_both_reference_forms():
    assert parse_key("Themes:Parody") == ("Themes", "Parody")
    assert parse_key("narrative") == (None, "narrative")


# -------------------------------------------------------------- the intents


def _facets(*values):
    return {Facet("SCENE", "Themes", value) for value in values}


def test_a_vetoed_title_is_disqualified_not_merely_unranked():
    """`none` is a veto, and the distinction from a zero score matters.

    If a veto scored zero instead of rejecting, a rail would fill with exactly
    the titles the intent rules out as soon as nothing else scored.
    """

    intent = Intent(name="t", label="t", any=["parody"], none=["amateur"])

    assert intent.evaluate(_facets("amateur", "parody")) is None
    assert intent.evaluate(_facets("parody")) == (1.0, ["parody"])


def test_a_required_term_must_be_present():
    intent = Intent(name="t", label="t", all=["parody"])

    assert intent.evaluate(_facets("drama")) is None
    assert intent.evaluate(_facets("parody")) is not None


def test_runtime_and_year_bounds_disqualify():
    intent = Intent(name="t", label="t", any=["drama"], min_runtime=80, max_year=1989)

    assert intent.evaluate(_facets("drama"), runtime=45, year=1980) is None
    assert intent.evaluate(_facets("drama"), runtime=95, year=2020) is None
    assert intent.evaluate(_facets("drama"), runtime=95, year=1980) is not None


def test_a_title_matching_nothing_the_intent_asks_for_is_excluded():
    """Not a weak match -- no match.

    Scoring it zero and keeping it is what made four rails come back
    byte-identical to the unfiltered one on the live catalogue: every title
    was "eligible" for every intent. A row labelled "Outdoors" that is really
    "everything" is worse than no row.
    """

    intent = Intent(name="t", label="t", any=["parody"])

    assert intent.evaluate(_facets("drama")) is None


def test_a_bound_only_excludes_a_value_it_actually_knows():
    """A bound says nothing about an unknown value; the `any` list filters.

    Making bounds strict instead threw away the entire award corpus at once,
    because award entries carry no runtime -- which is what emptied "Has a
    real plot". Era membership is kept honest by requiring a positive decade
    facet, not by treating a missing year as disqualifying.
    """

    intent = Intent(name="t", label="t", any=["drama"], min_runtime=80)

    assert intent.evaluate(_facets("drama"), runtime=None) is not None
    assert intent.evaluate(_facets("drama"), runtime=45) is None


def test_a_decade_facet_is_derived_only_where_stashdb_has_the_theme():
    """`Themes:1970s` through `1990s` exist in the tag graph; `2010s` does not,
    and a facet matching nothing reads as a bug."""

    assert engine_module.decade_facet(1978) == "1970s"
    assert engine_module.decade_facet(1985) == "1980s"
    assert engine_module.decade_facet(2020) is None
    assert engine_module.decade_facet(None) is None


def test_an_entry_takes_its_genres_from_the_storefront_categories():
    """The only real genre data in the movie corpus. A product page has none,
    so without this the theme rails have nothing to match on."""

    engine_module.category_index._index = {"999": ["Classic Plot", "Feature"]}

    try:
        facets = MovieEngine.entry_facets(_entry(external_id="999", year=1984))
        values = {f.value.lower() for f in facets}

        assert "classic plot" in values, values
        assert "feature" in values, values
        # And the decade the year implies, in StashDB's own vocabulary.
        assert "1980s" in values, values
    finally:
        engine_module.category_index._index = {}


def test_score_is_the_share_of_any_terms_matched():
    intent = Intent(name="t", label="t", any=["a", "b", "c", "d"])
    verdict = intent.evaluate(_facets("a", "b"))

    assert verdict is not None
    assert abs(verdict[0] - 0.5) < 1e-9, verdict


def test_every_default_intent_names_an_engine_it_suits():
    """"Real plot" against StashDB's scene corpus is a confident wrong answer."""

    for intent in DEFAULT_INTENTS:
        assert intent.engines, f"{intent.name} declares no engine"
        assert set(intent.engines) <= {"movies", "scenes"}, intent.name

    real_plot = next(i for i in DEFAULT_INTENTS if i.name == "real-plot")

    assert real_plot.engines == ["movies"], real_plot.engines


def test_overrides_merge_per_intent_rather_than_replacing_the_library(tmp_path):
    path = tmp_path / "intents.json"
    path.write_text(json.dumps([{"name": "outdoor", "min_runtime": 30}]))

    intents = IntentLibrary(path=path).intents

    assert intents["outdoor"].min_runtime == 30
    # Untouched fields survive, and the other defaults are still there.
    assert intents["outdoor"].any
    assert "real-plot" in intents


def test_an_unreadable_override_file_falls_back_to_the_defaults(tmp_path):
    path = tmp_path / "intents.json"
    path.write_text("{not json")

    assert len(IntentLibrary(path=path).intents) == len(DEFAULT_INTENTS)


# --------------------------------------------------------------- the engine


def _entry(**kwargs):
    defaults = dict(
        id=1,
        title="A Title",
        category=None,
        studio=None,
        performers=[],
        rating=None,
        rank=None,
        year=None,
        released_at=None,
        duration_minutes=None,
        media_item=None,
        media_item_id=None,
        collection=SimpleNamespace(key="adultempire-trending"),
        external_source="adultempire",
        external_id="123",
        tpdb_id=None,
        stashdb_id=None,
        adultempire_id=None,
        poster_path=None,
        requested=False,
    )
    defaults.update(kwargs)

    return SimpleNamespace(**defaults)


def test_the_award_category_is_read_as_a_genre_statement():
    """"Best Parody" is an editorial genre claim -- and the only tag-like fact
    a bare award row carries at all."""

    facets = MovieEngine.entry_facets(_entry(category="Best Parody Release"))
    values = {facet.value.lower() for facet in facets}

    assert "parody" in values, values
    # The scaffolding words are not facets.
    assert "best" not in values, values


def test_scoring_disqualifies_a_vetoed_entry():
    intent = Intent(name="t", label="t", any=["parody"], none=["amateur"])

    scored = MovieEngine()._score(
        _entry(category="Best Amateur Release"),
        intent=intent,
        taste=LibraryTaste(),
        awards={},
        mean_rating=0.0,
    )

    assert scored is None


def test_a_winner_outranks_a_nominee_of_otherwise_equal_standing():
    engine = MovieEngine()
    awards = {
        "winner": (AWARD_WEIGHTS["avn"], ["AVN 2020 Best Drama"]),
        "nominee": (AWARD_WEIGHTS["avn"] * NOMINEE_FRACTION, ["AVN 2020 Best Drama"]),
    }

    def score(title):
        return engine._score(
            _entry(title=title),
            intent=None,
            taste=LibraryTaste(),
            awards=awards,
            mean_rating=0.0,
        )

    won, nominated = score("Winner"), score("Nominee")

    assert won is not None and nominated is not None
    assert won.score > nominated.score, (won.score, nominated.score)


def test_a_lone_five_star_rating_does_not_beat_a_well_rated_corpus():
    """The prior is the point: Adult Empire publishes no vote count, so an
    unshrunk rating would let one rogue five-star row top every rail."""

    scored = MovieEngine()._score(
        _entry(rating=5.0),
        intent=None,
        taste=LibraryTaste(),
        awards={},
        mean_rating=2.0,
    )

    assert scored is not None
    assert scored.signals["rating"] < 1.0, scored.signals


def test_results_carry_the_signals_that_produced_them():
    scored = MovieEngine()._score(
        _entry(rating=4.5, rank=1, year=2020),
        intent=None,
        taste=LibraryTaste(),
        awards={},
        mean_rating=4.0,
    )

    assert scored is not None
    assert {"rating", "demand", "recency"} <= set(scored.signals), scored.signals


def test_library_affinity_reports_what_overlapped():
    taste = LibraryTaste(performers={"asa akira": 3}, studios={"wicked": 2})
    score, reasons = taste.affinity(performers=["Asa Akira"], studio="Wicked")

    assert score > 0
    assert any("Asa Akira" in reason for reason in reasons), reasons


def test_an_empty_taste_profile_contributes_nothing():
    assert LibraryTaste().affinity(performers=["Anyone"], studio="Anywhere") == (0.0, [])


# ---------------------------------------------------------- the scene engine


def test_the_scene_engine_reports_unavailable_without_an_ingested_graph():
    """A configured key alone is not enough. With no tag ids the query would
    quietly degrade into "newest scenes" wearing the intent's name."""

    facets_module.vocabulary._graph = TagGraph(by_value={}, ids={})
    engine = SceneEngine(api=SimpleNamespace(configured=True))

    assert engine.available is False
    assert engine.rank(DEFAULT_INTENTS[0]) == []


def test_the_scene_engine_will_not_query_an_intent_stashdb_cannot_express():
    facets_module.vocabulary._graph = TagGraph(
        by_value={}, ids={"romance": "uuid-1"}
    )

    def fail(*_args, **_kwargs):
        raise AssertionError("must not query StashDB with no resolvable tag")

    engine = SceneEngine(api=SimpleNamespace(configured=True, _query=fail))
    intent = Intent(name="t", label="t", any=["Themes:Nothing StashDB Knows"])

    assert engine.rank(intent) == []


def test_the_scene_engine_pulls_rather_than_requires_its_any_terms():
    """INCLUDES_ALL over eleven location tags demands a scene shot on a beach
    *and* a boat *and* a balcony, which returns nothing."""

    facets_module.vocabulary._graph = TagGraph(
        by_value={"outdoors": Facet("SCENE", "Locations", "Outdoors")},
        ids={"outdoors": "uuid-3"},
    )
    sent = {}

    def capture(_query, variables):
        sent.update(variables["input"])

        return {"queryScenes": {"scenes": []}}

    engine = SceneEngine(api=SimpleNamespace(configured=True, _query=capture))
    engine.rank(Intent(name="t", label="t", any=["Locations:Outdoors"]))

    assert sent["tags"]["modifier"] == "INCLUDES", sent["tags"]


import tempfile  # noqa: E402 - only the harness below needs it

for _name, _fn in sorted(list(globals().items())):
    if _name.startswith("test_") and callable(_fn):
        if "tmp_path" in _fn.__code__.co_varnames[: _fn.__code__.co_argcount]:
            with tempfile.TemporaryDirectory() as _dir:
                check(_name, lambda fn=_fn, d=_dir: fn(Path(d)))
        else:
            check(_name, _fn)

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")

for _name, _err in FAILED:
    print(f"  FAIL {_name}: {_err}")

sys.exit(1 if FAILED else 0)
