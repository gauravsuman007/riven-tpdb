"""AVN award corpus parsing and TPDB match scoring.

Stdlib-only and self-contained, following the other suites in this directory:
the modules under test are loaded directly by path so the whole program package
(and its FUSE/database dependencies) never has to import.
"""

import importlib.util
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

# loguru is the only third-party import in the modules under test.
if "loguru" not in sys.modules:
    stub = types.ModuleType("loguru")

    class _Logger:
        def __getattr__(self, _):
            return lambda *args, **kwargs: None

    stub.logger = _Logger()
    sys.modules["loguru"] = stub


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


AWARDS = SRC / "program" / "services" / "awards"
_load("program.services.awards.wikitable", AWARDS / "wikitable.py")
wikitable = sys.modules["program.services.awards.wikitable"]
avn = _load("avn_under_test", AWARDS / "avn.py")

# matching.py imports program.utils.text_matching, which is stdlib-only.
matching = _load("avn_matching_under_test", AWARDS / "matching.py")

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


# --------------------------------------------------------------- table parsing

MODERN = """
{| class="wikitable"
|-
| style="width:50%; vertical-align:top;" | {{Award category|#89cff0|Grand Reel}}
* '''Strip''' - ''Dorcel/Pulse''
** The Blueprint - ''Blacked/Pulse''
** Clout - ''Wicked/Pulse''
| style="width:50%; vertical-align:top;" | {{Award category|#89cff0|Best Actor}}
* '''[[Tommy Pistol]], "Mr. Sicko"''' - ''Kink''
** Dante Colle, "The Pitch" - ''Bellesa''
|}
"""

# The older layout: categories in a header row, entries in the row *below*.
LEGACY = """
{| class=wikitable
|-
! style="background:#89cff0" width="50%" | Best Film
! style="background:#89cff0" width="50%" | Best Actress
|-
| valign="top" |
* '''''Face Dance, Parts 1 & 2'''''{{double-dagger}}<ref name="AVN-mag" />
** ''Bonnie and Clyde''
| valign="top" |
* '''Colleen Brennan, Getting Personal'''<ref name="AVN-mag" />
|}
"""

INLINE_LIST = """
=== Additional award winners ===
{{Col-begin}}
{{Col-1-of-2}}
* '''Best All-Girl Video:''' ''Kittens III''<ref name="AVN-mag" />
* '''Best Web Retail Store:''' ''Adam & Eve''
"""


def test_modern_layout():
    entries = avn.parse_ceremony(43, MODERN)
    by_cat = {}

    for e in entries:
        by_cat.setdefault(e.category, []).append(e)

    assert set(by_cat) == {"Grand Reel", "Best Actor"}, f"categories: {list(by_cat)}"

    grand = by_cat["Grand Reel"]
    winner = [e for e in grand if e.winner]
    assert len(winner) == 1, f"expected 1 winner, got {len(winner)}"
    assert winner[0].title == "Strip", winner[0].title
    assert winner[0].studio == "Dorcel/Pulse", winner[0].studio
    assert len([e for e in grand if not e.winner]) == 2


def test_modern_person_category_extracts_quoted_work():
    entries = avn.parse_ceremony(43, MODERN)
    actor = [e for e in entries if e.category == "Best Actor" and e.winner][0]

    assert actor.title == "Mr. Sicko", actor.title
    assert actor.performers == ["Tommy Pistol"], actor.performers
    assert actor.studio == "Kink", actor.studio


def test_legacy_layout_attributes_categories_positionally():
    """The defect a flat regex has: entries land under the previous category."""

    entries = avn.parse_ceremony(10, LEGACY)
    winners = {e.category: e for e in entries if e.winner}

    assert "Best Film" in winners, list(winners)
    assert winners["Best Film"].title == "Face Dance, Parts 1 & 2", winners["Best Film"].title
    assert winners["Best Actress"].title == "Getting Personal", winners["Best Actress"].title
    assert winners["Best Actress"].performers == ["Colleen Brennan"]


def test_refs_are_not_mistaken_for_titles():
    """<ref name="AVN-mag" /> looks exactly like a quoted work title."""

    entries = avn.parse_ceremony(10, LEGACY)

    for entry in entries:
        assert entry.title != "AVN-mag", f"ref leaked into title: {entry}"
        assert "ref" not in (entry.raw or "").lower() or "referen" in entry.raw.lower()


def test_bold_markers_stripped():
    entries = avn.parse_ceremony(43, MODERN)

    for entry in entries:
        assert "'''" not in (entry.title or ""), entry.title
        assert "''" not in (entry.studio or ""), entry.studio


def test_inline_category_lists_are_parsed():
    entries = avn.parse_ceremony(10, INLINE_LIST)
    titles = {e.category: e.title for e in entries}

    assert titles.get("Best All-Girl Video") == "Kittens III", titles


def test_non_media_categories_excluded():
    """Retail and web categories name businesses, not titles."""

    entries = avn.parse_ceremony(10, INLINE_LIST)

    assert not any("Retail" in (e.category or "") for e in entries), [
        e.category for e in entries
    ]


def test_nested_table_does_not_split_a_cell():
    nested = MODERN.replace(
        "** Clout - ''Wicked/Pulse''",
        "** Clout - ''Wicked/Pulse''\n{| class=\"inner\"\n| junk\n|}",
    )
    entries = avn.parse_ceremony(43, nested)

    assert any(e.title == "Strip" and e.winner for e in entries)


def test_duplicate_entries_deduped():
    doubled = MODERN + INLINE_LIST + INLINE_LIST
    entries = avn.parse_ceremony(43, doubled)
    keys = [(e.category, e.title, e.winner) for e in entries]

    assert len(keys) == len(set(keys)), "duplicate entries survived"


def test_ceremony_year_mapping():
    assert avn.ceremony_year(43) == 2026, avn.ceremony_year(43)
    assert avn.ceremony_year(4) == 1987, avn.ceremony_year(4)
    assert avn.article_title(41) == "41st AVN Awards"


# -------------------------------------------------------------------- matching


def _candidate(**kwargs):
    base = dict(
        entry_title="Strip",
        entry_studio="Dorcel/Pulse",
        entry_year=2026,
        entry_performers=["Tommy Pistol"],
        tpdb_id="uuid-1",
        tpdb_kind="movie",
        tpdb_title="Strip",
        tpdb_site="Dorcel",
        tpdb_date="2025-06-01",
        tpdb_performers=["Tommy Pistol"],
    )
    base.update(kwargs)
    return matching.evaluate_candidate(**base)


def test_full_agreement_accepted():
    match = _candidate()

    assert match.accepted, f"score {match.score}: {match.reasons}"
    assert match.studio and match.performers == 1


def test_title_alone_is_never_enough():
    """A bare title match is how the wrong film gets into a collection."""

    match = _candidate(
        tpdb_site="Unrelated Studio", tpdb_performers=[], tpdb_date=None
    )

    assert not match.accepted, f"accepted on title alone: {match.score}"


def test_volume_conflict_rejected():
    match = _candidate(
        entry_title="Anal Savages 11",
        tpdb_title="Anal Savages 3",
        tpdb_site="Jules Jordan Video",
        entry_studio="Jules Jordan Video",
    )

    assert match.volume_conflict, match.reasons
    assert not match.accepted
    assert match.score == 0.0


def test_year_offset_uses_release_year_not_ceremony_year():
    """Ceremony 2026 honours work released in 2025."""

    match = _candidate(tpdb_date="2025-01-01")

    assert match.year_delta == 0, match.year_delta


def test_studio_matches_on_any_component():
    match = _candidate(entry_studio="Girlcore/Adult Time/Pulse", tpdb_site="Adult Time")

    assert match.studio, match.reasons


def test_best_match_picks_highest_score():
    weak = _candidate(tpdb_id="weak", tpdb_site="Other", tpdb_performers=[])
    strong = _candidate(tpdb_id="strong")
    best = matching.best_match([weak, strong])

    assert best is not None and best.tpdb_id == "strong", best


def test_best_match_returns_none_when_nothing_accepted():
    weak = _candidate(
        tpdb_title="Something Else Entirely",
        tpdb_site="Other",
        tpdb_performers=[],
        tpdb_date=None,
    )

    assert matching.best_match([weak]) is None


for _name, _fn in sorted(list(globals().items())):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")

for _name, _err in FAILED:
    print(f"  FAIL {_name}: {_err}")

sys.exit(1 if FAILED else 0)
