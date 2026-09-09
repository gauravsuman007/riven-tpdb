"""Library search facets: the cast table and how it is kept in step.

Runs against a real in-memory SQLite database with a minimal stand-in for
`MediaItem`, so the FUSE-dependent program package is never imported. Skips
cleanly without SQLAlchemy.
"""

import importlib.util
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

try:
    import sqlalchemy
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
except ImportError:  # pragma: no cover - environment without the app deps
    print("SKIP: sqlalchemy not installed")
    sys.exit(0)


def _module(name, **attrs):
    mod = types.ModuleType(name)

    for key, value in attrs.items():
        setattr(mod, key, value)

    sys.modules[name] = mod
    return mod


class Base(DeclarativeBase):
    pass


class MediaItem(Base):
    """Only the columns the facet table reads."""

    __tablename__ = "MediaItem"

    id: Mapped[int] = mapped_column(sqlalchemy.Integer, primary_key=True)
    title: Mapped[str] = mapped_column(sqlalchemy.String, nullable=True)
    site_name: Mapped[str] = mapped_column(sqlalchemy.String, nullable=True)
    performers = mapped_column(sqlalchemy.JSON, nullable=True)


_module("program")
_module("program.db")
_module("program.db.base_model", Base=Base)
_module("program.media")
_module("program.media.item", MediaItem=MediaItem)

_spec = importlib.util.spec_from_file_location(
    "item_performer_under_test",
    SRC / "program" / "media" / "item_performer.py",
)
ip = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ip
_spec.loader.exec_module(ip)

ENGINE = create_engine("sqlite://")
Base.metadata.create_all(ENGINE)


PASSED, FAILED = [], []


def check(name, fn):
    try:
        fn()
    except AssertionError as exc:
        FAILED.append((name, str(exc)))
    except Exception as exc:  # noqa: BLE001
        import traceback

        FAILED.append((name, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"))
    else:
        PASSED.append(name)


def _reset(session):
    session.query(ip.ItemPerformer).delete()
    session.query(MediaItem).delete()
    session.commit()


def _names_for(session, item_id):
    return set(
        session.execute(
            select(ip.ItemPerformer.name).where(
                ip.ItemPerformer.media_item_id == item_id
            )
        )
        .scalars()
        .all()
    )


def test_normalise_folds_case_and_whitespace():
    assert ip.normalise_name("  Riley   Reid ") == "riley reid"


def test_clean_names_drops_junk_and_duplicates():
    cleaned = ip.clean_names(["Riley Reid", "riley reid", "", None, 7, {"a": 1}])

    assert cleaned == [("Riley Reid", "riley reid")], cleaned


def test_clean_names_tolerates_a_non_list():
    # The column is free-form JSON: a bad upstream payload must not be able
    # to fail the flush that saves the title.
    assert ip.clean_names(None) == []
    assert ip.clean_names("Riley Reid") == []
    assert ip.clean_names({"performers": []}) == []


def test_insert_populates_the_cast():
    with Session(ENGINE) as s:
        _reset(s)
        item = MediaItem(
            id=1, title="Pirates", site_name="Digital Playground",
            performers=["Jesse Jane", "Janine Lindemulder"],
        )
        s.add(item)
        s.commit()

        assert _names_for(s, 1) == {"Jesse Jane", "Janine Lindemulder"}


def test_update_replaces_the_cast():
    with Session(ENGINE) as s:
        _reset(s)
        s.add(MediaItem(id=1, title="Pirates", performers=["Jesse Jane"]))
        s.commit()

        item = s.get(MediaItem, 1)
        item.performers = ["Janine Lindemulder"]
        s.commit()

        assert _names_for(s, 1) == {"Janine Lindemulder"}


def test_unrelated_update_leaves_the_cast_alone():
    """Items are updated on every pipeline state change.

    If any update rewrote the cast, one state transition would become a
    delete plus three inserts for every title in flight.
    """

    with Session(ENGINE) as s:
        _reset(s)
        s.add(MediaItem(id=1, title="Pirates", performers=["Jesse Jane"]))
        s.commit()

        before = s.execute(
            select(ip.ItemPerformer.id).where(ip.ItemPerformer.media_item_id == 1)
        ).scalars().all()

        item = s.get(MediaItem, 1)
        item.title = "Pirates (2005)"
        s.commit()

        after = s.execute(
            select(ip.ItemPerformer.id).where(ip.ItemPerformer.media_item_id == 1)
        ).scalars().all()

        assert before == after, (before, after)


def test_clearing_performers_empties_the_cast():
    with Session(ENGINE) as s:
        _reset(s)
        s.add(MediaItem(id=1, title="Pirates", performers=["Jesse Jane"]))
        s.commit()

        item = s.get(MediaItem, 1)
        item.performers = None
        s.commit()

        assert _names_for(s, 1) == set()


def test_rebuild_all_is_idempotent():
    with Session(ENGINE) as s:
        _reset(s)
        s.add(MediaItem(id=1, title="A", performers=["Jesse Jane", "jesse jane"]))
        s.add(MediaItem(id=2, title="B", performers=["Janine Lindemulder"]))
        s.commit()

        first = ip.rebuild_all(s)
        s.commit()
        second = ip.rebuild_all(s)
        s.commit()

        # The duplicate spelling collapses to one row, both times.
        assert first == second == 2, (first, second)


def test_a_deleted_item_takes_its_cast_with_it():
    with Session(ENGINE) as s:
        _reset(s)
        s.add(MediaItem(id=1, title="Pirates", performers=["Jesse Jane"]))
        s.commit()

        # SQLite needs foreign keys switched on explicitly; Postgres, where
        # this actually runs, enforces the ON DELETE CASCADE by default.
        s.execute(sqlalchemy.text("PRAGMA foreign_keys=ON"))
        s.execute(sqlalchemy.text('DELETE FROM "MediaItem" WHERE id = 1'))
        s.commit()

        assert _names_for(s, 1) == set()


def test_suggestion_grouping_collapses_casing():
    """What the suggest endpoint's performer query does, in miniature."""

    with Session(ENGINE) as s:
        _reset(s)
        s.add(MediaItem(id=1, title="A", performers=["Jesse Jane"]))
        s.add(MediaItem(id=2, title="B", performers=["jesse jane"]))
        s.commit()

        rows = s.execute(
            select(
                func.min(ip.ItemPerformer.name),
                func.count(func.distinct(ip.ItemPerformer.media_item_id)),
            )
            .where(ip.ItemPerformer.name_normalized.like("%jess%"))
            .group_by(ip.ItemPerformer.name_normalized)
        ).all()

        assert rows == [("Jesse Jane", 2)], rows


for _name, _fn in sorted(list(globals().items())):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")

for _name, _err in FAILED:
    print(f"  FAIL {_name}: {_err}")

sys.exit(1 if FAILED else 0)
