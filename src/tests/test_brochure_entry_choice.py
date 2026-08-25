"""Which cached brochure row a manual scrape is built from.

The same Adult Empire title appears in several shelves, and only shelves that
have been through the detail-enrichment pass carry studio, cast and release
date. Picking an unenriched row leaves the adult relevance filter no evidence
to work with, so every candidate is rejected and the scrape returns nothing --
which is exactly what happened to "Pirates".
"""

import sys
import types
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

try:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import DeclarativeBase, Session
except ImportError:  # pragma: no cover - environment without the app deps
    print("SKIP: sqlalchemy not installed")
    sys.exit(0)


class _Logger:
    def __getattr__(self, _):
        return lambda *args, **kwargs: None


_loguru = types.ModuleType("loguru")
_loguru.logger = _Logger()
sys.modules["loguru"] = _loguru


class Base(DeclarativeBase):
    pass


# Stub the package chain but keep __path__ pointing at the real directories,
# so program.media.collection still imports from source while
# program.db.base_model can be replaced with a bare declarative Base.
for pkg, rel in (("program", "program"), ("program.db", "program/db"),
                 ("program.media", "program/media")):
    mod = types.ModuleType(pkg)
    mod.__path__ = [str(SRC / rel)]
    sys.modules[pkg] = mod

_base_model = types.ModuleType("program.db.base_model")
_base_model.Base = Base
sys.modules["program.db.base_model"] = _base_model

import sqlalchemy  # noqa: E402
from sqlalchemy.orm import Mapped, mapped_column  # noqa: E402


class MediaItem(Base):
    """Only present so CollectionEntry's foreign key resolves."""

    __tablename__ = "MediaItem"

    id: Mapped[int] = mapped_column(sqlalchemy.Integer, primary_key=True)


from program.media.collection import Collection, CollectionEntry  # noqa: E402

# The ordering is duplicated here rather than imported, because importing the
# indexer drags in the FUSE-dependent program package. test_source_matches_-
# implementation is what keeps the copy honest.
from sqlalchemy import select  # noqa: E402

SOURCE = "adultempire"


def best_entry(session, external_id):
    return session.execute(
        select(CollectionEntry)
        .where(
            CollectionEntry.external_source == SOURCE,
            CollectionEntry.external_id == external_id,
        )
        .order_by(
            CollectionEntry.studio.is_(None),
            CollectionEntry.performers.is_(None),
            CollectionEntry.released_at.is_(None),
            CollectionEntry.year.is_(None),
            CollectionEntry.id,
        )
    ).scalars().first()


def test_source_matches_implementation():
    """The copy above must stay identical to the shipped ordering."""

    path = SRC / "program/services/indexers/adultempire_indexer.py"
    text = path.read_text()
    start = text.index("def best_entry(")
    end = text.index("def build_movie(")
    body = text[start:end]

    for clause in (
        "CollectionEntry.studio.is_(None)",
        "CollectionEntry.performers.is_(None)",
        "CollectionEntry.released_at.is_(None)",
        "CollectionEntry.year.is_(None)",
    ):
        assert clause in body, f"{clause} missing from shipped best_entry"


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return Session(engine)


def _shelf(session, key, **entry):
    collection = Collection(key=key, source=SOURCE, name=key)
    session.add(collection)
    session.flush()

    row = CollectionEntry(
        collection_id=collection.id,
        external_source=SOURCE,
        external_id="700215",
        title="Pirates",
        **entry,
    )
    session.add(row)
    session.flush()

    return row


def test_enriched_row_wins_even_when_added_later():
    session = _session()

    bare = _shelf(session, "adultempire-trending")
    rich = _shelf(
        session,
        "adultempire-all-time-bestsellers",
        studio="Digital Playground",
        year=2005,
        released_at=datetime(2005, 9, 26),
        performers=["Jesse Jane"],
    )

    chosen = best_entry(session, "700215")

    assert chosen.id == rich.id, "picked the row with no studio or cast"
    assert chosen.id != bare.id


def test_enriched_row_wins_when_added_first():
    """Ordering must not depend on insertion order."""

    session = _session()

    rich = _shelf(
        session,
        "adultempire-all-time-bestsellers",
        studio="Digital Playground",
        year=2005,
        released_at=datetime(2005, 9, 26),
        performers=["Jesse Jane"],
    )
    _shelf(session, "adultempire-trending")

    assert best_entry(session, "700215").id == rich.id


def test_partially_enriched_beats_bare():
    session = _session()

    _shelf(session, "adultempire-trending")
    partial = _shelf(session, "adultempire-bestsellers", studio="Digital Playground")

    assert best_entry(session, "700215").id == partial.id


def test_bare_row_is_still_returned_when_it_is_all_there_is():
    session = _session()

    bare = _shelf(session, "adultempire-trending")

    assert best_entry(session, "700215").id == bare.id


def test_choice_is_stable_across_calls():
    session = _session()

    _shelf(session, "adultempire-trending")
    _shelf(session, "adultempire-bestsellers")
    rich = _shelf(session, "adultempire-all-time-bestsellers", studio="DP", year=2005)

    assert {best_entry(session, "700215").id for _ in range(5)} == {rich.id}


def test_unknown_id_returns_none():
    session = _session()
    _shelf(session, "adultempire-trending")

    assert best_entry(session, "999999") is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

    failed = 0

    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")

    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
