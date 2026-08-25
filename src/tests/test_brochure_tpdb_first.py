"""A storefront title enters the library as an ordinary TPDB title.

The point of resolving at the boundary is that everything downstream --
indexing, scraping, the detail page -- is the same code the rest of the fork
uses. What has to be protected is the shape of that boundary:

    * a match produces a payload addressed by tpdb_id, not by the storefront id
    * a miss still produces a usable payload rather than an error
    * the resolved id is written back, so the lookup happens once
"""

import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

try:
    import sqlalchemy
    from sqlalchemy import create_engine
    from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
except ImportError:  # pragma: no cover
    print("SKIP: sqlalchemy not installed")
    sys.exit(0)


class _Logger:
    def __getattr__(self, _):
        return lambda *args, **kwargs: None


sys.modules["loguru"] = types.ModuleType("loguru")
sys.modules["loguru"].logger = _Logger()
sys.modules["kink"] = types.ModuleType("kink")
sys.modules["kink"].di = {}


class Base(DeclarativeBase):
    pass


class MediaItem(Base):
    __tablename__ = "MediaItem"
    id: Mapped[int] = mapped_column(sqlalchemy.Integer, primary_key=True)


for pkg, rel in (("program", "program"), ("program.db", "program/db"),
                 ("program.media", "program/media")):
    mod = types.ModuleType(pkg)
    mod.__path__ = [str(SRC / rel)]
    sys.modules[pkg] = mod

_bm = types.ModuleType("program.db.base_model")
_bm.Base = Base
sys.modules["program.db.base_model"] = _bm

from program.media.collection import Collection, CollectionEntry  # noqa: E402


MATCH_MATCHED = "matched"


class _Match:
    """What resolve_movie returns on a hit."""

    def __init__(self):
        self.tpdb_id = "uuid-pirates"
        self.kind = "movie"
        self.score = 9.5
        self.poster = "https://example/poster.jpg"


def enrich(entry, match):
    """The shipped enrich_entry, with the network call substituted.

    Only the write-back is mirrored; the matching itself is covered by
    test_awards.py, which is where the scorer lives.
    """

    from datetime import datetime

    if entry.tpdb_id or not entry.title:
        return False

    if match is None:
        return False

    entry.tpdb_id = match.tpdb_id
    entry.tpdb_kind = match.kind
    entry.match_score = match.score
    entry.match_state = MATCH_MATCHED
    entry.matched_at = datetime.now()

    if match.poster:
        entry.poster_path = match.poster

    return True


def payload_for(entry):
    """The branch request_entry takes, mirrored."""

    if entry.tpdb_id:
        return {"tpdb_id": entry.tpdb_id}

    return {"adultempire_id": entry.external_id}


def _entry(session, **kwargs):
    collection = Collection(key="adultempire-bestsellers", source="adultempire",
                            name="Bestsellers")
    session.add(collection)
    session.flush()

    row = CollectionEntry(
        collection_id=collection.id,
        external_source="adultempire",
        external_id="700215",
        title="Pirates",
        studio="Digital Playground",
        year=2005,
        **kwargs,
    )
    session.add(row)
    session.flush()

    return row


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_a_match_is_requested_as_a_tpdb_title():
    session = _session()
    entry = _entry(session)

    assert enrich(entry, _Match()) is True
    assert payload_for(entry) == {"tpdb_id": "uuid-pirates"}, (
        "a resolved title must enter the library by its TPDB id, or it takes "
        "the storefront path and nothing downstream is shared"
    )


def test_a_miss_still_produces_a_usable_payload():
    """One title in five has no confident match; those must stay downloadable."""

    session = _session()
    entry = _entry(session)

    assert enrich(entry, None) is False
    assert payload_for(entry) == {"adultempire_id": "700215"}


def test_the_resolved_id_is_written_back():
    """So the second request costs no TPDB round trip."""

    session = _session()
    entry = _entry(session)

    enrich(entry, _Match())
    session.commit()

    with Session(session.get_bind()) as fresh:
        stored = fresh.execute(sqlalchemy.select(CollectionEntry)).scalars().one()

        assert stored.tpdb_id == "uuid-pirates"
        assert stored.match_state == MATCH_MATCHED
        assert enrich(stored, _Match()) is False, "resolved twice"


def test_a_match_carries_the_poster_over():
    session = _session()
    entry = _entry(session)

    enrich(entry, _Match())

    assert entry.poster_path == "https://example/poster.jpg"


def test_request_resolves_before_choosing_a_payload():
    """Guard the ordering in the shipped router.

    Reading entry.tpdb_id before enriching would take the storefront branch
    for every title that had not already been matched by something else --
    the exact divergence this change removes.
    """

    text = (SRC / "routers/secure/collections.py").read_text()
    body = text[text.index("def request_entry("):]
    body = body[: body.index("class BrochureShelf")]

    enrich_at = body.index("enrich_entry(entry)")
    branch_at = body.index("if entry.tpdb_id:")

    assert enrich_at < branch_at, (
        "request_entry branches on tpdb_id before resolving it, so an "
        "unresolved title never takes the TPDB path"
    )


def test_manual_scrape_resolves_before_falling_back():
    """Same ordering guard for the scrape boundary."""

    text = (SRC / "routers/secure/scrape.py").read_text()
    body = text[text.index("def resolve_media_item("):]
    body = body[: body.index("# If item not found locally")]

    enrich_at = body.index("enrich_entry(entry)")
    fallback_at = body.index("build_adultempire_movie(entry)")

    assert enrich_at < fallback_at, (
        "resolve_media_item builds a storefront item before trying TPDB"
    )


def test_resolution_does_not_re_ask_about_known_misses():
    """Guard the filter in the shipped resolve_batch.

    About one title in five has no TPDB record at all. Selecting on
    "tpdb_id is null" alone would hand those same entries back on every run
    forever, spending the entire TPDB rate limit re-confirming misses and
    starving the entries that have never been tried. The `matched_at` clause
    is what makes an attempt stick, so it is worth a test of its own.
    """

    text = (SRC / "program/services/recommendations/brochure.py").read_text()
    body = text[text.index("def resolve_batch("):]

    assert "CollectionEntry.matched_at.is_(None)" in body, (
        "resolve_batch selects entries without recording that they were "
        "attempted, so every known miss is retried on every run"
    )


def test_a_miss_stays_requestable():
    """A failed TPDB lookup must not take away a working title.

    The tempting move is to mark a miss `unmatched`, as the awards path does.
    That would be wrong here: an award entry with no TPDB record is a dead
    row, but a storefront entry carries its own studio, year and cast and is
    downloadable on those alone. `actionable` is the thing that must survive.
    """

    from datetime import datetime

    session = _session()
    entry = _entry(session, match_state="self_sourced")

    # What resolve_batch does on a miss.
    assert enrich(entry, None) is False
    entry.matched_at = datetime.now()

    assert entry.match_state == "self_sourced"
    assert entry.actionable is True, (
        "a title TPDB could not match must still be requestable from the "
        "storefront metadata it already has"
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
