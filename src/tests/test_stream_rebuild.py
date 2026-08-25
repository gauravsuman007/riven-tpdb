"""Rebuilding a Stream row from a remembered manual-scrape result.

Picking a release the debrid service does not hold yet has to recreate the
Stream server-side, from the candidate list the scrape produced rather than
from anything the browser sends back. That rebuild bypasses Stream.__init__,
which takes an RTN Torrent nobody still has -- and bypassing it is exactly
where it went wrong the first time.
"""

import sys
import types
from types import SimpleNamespace
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

try:
    import sqlalchemy
    from sqlalchemy import create_engine
    from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
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


class Stream(Base):
    """The columns the rebuild actually copies.

    Deliberately not the real model: importing it drags in the FUSE-dependent
    program package. What is under test is the *mechanism* -- that an instance
    built without calling __init__ is a usable ORM object -- and that is a
    property of SQLAlchemy's instrumentation, not of these particular columns.
    """

    __tablename__ = "Stream"

    id: Mapped[int] = mapped_column(sqlalchemy.Integer, primary_key=True)
    infohash: Mapped[str] = mapped_column(sqlalchemy.String)
    raw_title: Mapped[str] = mapped_column(sqlalchemy.String)
    parsed_title: Mapped[str] = mapped_column(sqlalchemy.String, nullable=True)
    rank: Mapped[int] = mapped_column(sqlalchemy.Integer, nullable=True)
    lev_ratio: Mapped[float] = mapped_column(sqlalchemy.Float, nullable=True)
    resolution: Mapped[str] = mapped_column(sqlalchemy.String, nullable=True)
    seeders: Mapped[int] = mapped_column(sqlalchemy.Integer, nullable=True)
    leechers: Mapped[int] = mapped_column(sqlalchemy.Integer, nullable=True)
    size: Mapped[int] = mapped_column(sqlalchemy.BigInteger, nullable=True)
    indexer: Mapped[str] = mapped_column(sqlalchemy.String, nullable=True)
    is_cached: Mapped[bool] = mapped_column(sqlalchemy.Boolean, default=False)

    def __init__(self, torrent, result=None):
        """Mirrors the real model: construction requires an RTN Torrent."""

        self.infohash = torrent.infohash
        self.raw_title = torrent.raw_title
        self.parsed_title = torrent.data.parsed_title
        self.rank = torrent.rank
        self.lev_ratio = torrent.lev_ratio
        self.resolution = torrent.data.resolution
        self.is_cached = False

        if result is not None:
            self.seeders = result.seeders
            self.leechers = result.leechers
            self.size = result.size
            self.indexer = result.indexer


REMEMBERED = {
    "infohash": "96824e1723dda1e4acaaa071416d03ead3a37b1b",
    "raw_title": "Island Fever 3 (Digital Playground) WEB-DL",
    "parsed_data": SimpleNamespace(parsed_title="Island Fever 3", resolution="1080p"),
    "rank": 650,
    "lev_ratio": 0.94,
    "seeders": 3,
    "leechers": 1,
    "size": 8_000_000_000,
    "indexer": "Knaben",
}


def rebuild(remembered):
    """The shipped mechanism, mirrored."""

    torrent = SimpleNamespace(
        raw_title=remembered["raw_title"],
        infohash=remembered["infohash"],
        data=remembered["parsed_data"],
        rank=remembered["rank"],
        lev_ratio=remembered["lev_ratio"],
    )
    result = SimpleNamespace(
        seeders=remembered["seeders"],
        leechers=remembered["leechers"],
        size=remembered["size"],
        indexer=remembered["indexer"],
    )

    stream = Stream(torrent, result)
    stream.is_cached = False

    return stream


def test_rebuild_does_not_need_a_torrent():
    """__init__ takes a Torrent; the rebuild must not have to invent one."""

    stream = rebuild(REMEMBERED)

    assert stream.infohash == REMEMBERED["infohash"]
    assert stream.raw_title == REMEMBERED["raw_title"]


def test_rebuilt_stream_is_a_usable_orm_instance():
    """The regression: __new__ leaves no _sa_instance_state and raises."""

    stream = rebuild(REMEMBERED)

    assert hasattr(stream, "_sa_instance_state"), (
        "rebuilt Stream has no SQLAlchemy state, so assigning any column "
        "raises AttributeError before it can be persisted"
    )


def test_rebuilt_stream_persists():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(rebuild(REMEMBERED))
        session.commit()

        stored = session.execute(sqlalchemy.select(Stream)).scalars().one()

        assert stored.indexer == "Knaben"
        assert stored.seeders == 3
        assert stored.is_cached is False, "a rebuilt pick must not claim to be cached"


def test_each_rebuild_is_a_separate_row():
    """One cached entry may be picked for two different titles."""

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        first, second = rebuild(REMEMBERED), rebuild(REMEMBERED)

        assert first is not second

        session.add_all([first, second])
        session.commit()

        assert first.id != second.id


def test_source_uses_the_real_constructor():
    """Guard the mechanism the shipped code actually uses."""

    text = (SRC / "routers/secure/scrape.py").read_text()
    start = text.index("def _rebuild_stream(")
    body = text[start:text.index("\n\n\n", start)]

    assert "ItemStream(torrent, result)" in body, (
        "_rebuild_stream no longer goes through the real constructor; "
        "building the instance any other way skips SQLAlchemy's "
        "instrumentation and it raises on the first column assignment"
    )


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0

    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")

    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
