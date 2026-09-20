"""Fuzzy name matching: the misses that made this necessary, and precision.

Runs against in-memory SQLite, so the trigram half is absent by design --
which is the point of one of the tests. Stdlib harness, like the other
self-contained suites here. Run directly:
``python3 src/tests/test_fuzzy_search.py``.
"""

import importlib.util
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

try:
    from sqlalchemy import String, create_engine, select
    from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
except ImportError:  # pragma: no cover - environment without the app deps
    print("SKIP: sqlalchemy not installed")
    sys.exit(0)

spec = importlib.util.spec_from_file_location(
    "fuzzy", SRC / "program/utils/fuzzy.py"
)
fuzzy = importlib.util.module_from_spec(spec)
sys.modules["fuzzy"] = fuzzy
spec.loader.exec_module(fuzzy)

PASSED, FAILED = [], []


def check(name, fn):
    try:
        fn()
        PASSED.append(name)
        print(f"  ok   {name}")
    except Exception as error:  # noqa: BLE001 - report, keep going
        FAILED.append((name, repr(error)))
        print(f"  FAIL {name}")


class Base(DeclarativeBase):
    pass


class Studio(Base):
    __tablename__ = "studio"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    collapsed: Mapped[str] = mapped_column(String)


NAMES = [
    "Brazzers",
    "Evil Angel",
    "Evil Angel - Rocco Siffredi",
    "Vixen",
    "Exotic Vixen Films",
    "Pure Taboo",
    "Bratty Sis",
]


def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    s.add_all(
        Studio(id=i, name=n, collapsed=fuzzy.collapse(n))
        for i, n in enumerate(NAMES, start=1)
    )
    s.commit()
    return s


def found(term):
    s = session()
    query = select(Studio).where(
        fuzzy.matches(
            term, Studio.name, session=s, collapsed=Studio.collapsed
        )
    )
    order = fuzzy.ranking(
        term, Studio.name, session=s, collapsed=Studio.collapsed
    )
    return [
        row.name for row in s.execute(query.order_by(*order)).scalars().all()
    ]


# --- the measured misses that started this -------------------------------


def test_a_missing_space_still_finds_the_studio():
    """'evilangel' returned NOTHING before collapsing existed."""

    assert "Evil Angel" in found("evilangel")


def test_a_collapsed_query_ranks_the_exact_studio_first():
    assert found("evilangel")[0] == "Evil Angel"


def test_punctuation_in_the_query_is_ignored():
    assert "Evil Angel" in found("evil-angel")


def test_an_ordinary_substring_still_works():
    assert found("brazzers") == ["Brazzers"]


# --- precision: widening the net must not cost the top of the list -------


def test_the_exact_name_outranks_a_longer_one_containing_it():
    """'vixen' must offer Vixen before Exotic Vixen Films."""

    assert found("vixen")[:2] == ["Vixen", "Exotic Vixen Films"]


def test_a_prefix_outranks_a_mere_substring():
    assert found("evil angel")[0] == "Evil Angel"


def test_an_unrelated_term_matches_nothing():
    assert found("nonsensequery") == []


def test_an_empty_term_filters_nothing_out():
    assert len(found("")) == len(NAMES)


# --- the trigram half is optional, and this is the database without it ---


def test_sqlite_has_no_trigrams_and_still_answers():
    """Correctness must never depend on the extension being present."""

    s = session()

    assert not fuzzy.has_trigrams(s)
    assert "Brazzers" in found("brazzers")


def test_a_short_term_is_a_prefix_being_typed_not_a_typo():
    """Two letters must not fuzzy-match half the directory."""

    assert all(len(fuzzy.collapse(n)) >= 0 for n in found("vi"))
    assert "Vixen" in found("vi")
    assert "Brazzers" not in found("vi")


def test_collapse_strips_everything_but_letters_and_digits():
    assert fuzzy.collapse("Evil-Angel 2!") == "evilangel2"


for _name, _fn in sorted(list(globals().items())):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")

for _name, _err in FAILED:
    print(f"  FAIL {_name}: {_err}")

sys.exit(1 if FAILED else 0)
