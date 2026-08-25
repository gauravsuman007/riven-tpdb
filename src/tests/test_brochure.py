"""Adult Empire brochure: caching, indexing without TPDB, and requesting.

Runs against a real in-memory database; skips cleanly without SQLAlchemy.
The point under test is that a brochure title is fully actionable on Adult
Empire's own metadata -- no TPDB call anywhere in the request-to-scrape path.
"""

import importlib.util
import sys
import types
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

try:
    import sqlalchemy
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
except ImportError:  # pragma: no cover
    print("SKIP: sqlalchemy not installed")
    sys.exit(0)


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


class Base(DeclarativeBase):
    pass


class MediaItem(Base):
    __tablename__ = "MediaItem"

    id: Mapped[int] = mapped_column(sqlalchemy.Integer, primary_key=True)
    tpdb_id: Mapped[str] = mapped_column(sqlalchemy.String, nullable=True)
    adultempire_id: Mapped[str] = mapped_column(sqlalchemy.String, nullable=True)
    title: Mapped[str] = mapped_column(sqlalchemy.String, nullable=True)
    year: Mapped[int] = mapped_column(sqlalchemy.Integer, nullable=True)
    aired_at: Mapped[datetime] = mapped_column(sqlalchemy.DateTime, nullable=True)
    site_name: Mapped[str] = mapped_column(sqlalchemy.String, nullable=True)
    performers: Mapped[list] = mapped_column(sqlalchemy.JSON, nullable=True)
    poster_path: Mapped[str] = mapped_column(sqlalchemy.String, nullable=True)
    requested_by: Mapped[str] = mapped_column(sqlalchemy.String, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(sqlalchemy.DateTime, nullable=True)

    def __init__(self, payload=None, **kwargs):
        super().__init__(**(payload or kwargs))

    @property
    def is_adult(self) -> bool:
        return bool(self.tpdb_id or self.adultempire_id)

    @property
    def log_string(self) -> str:
        return self.title or str(self.id)


class Movie(MediaItem):
    pass


for pkg in ("program", "program.db", "program.media", "program.core",
            "program.services", "program.services.indexers",
            "program.services.recommendations", "program.settings"):
    sys.modules.setdefault(pkg, types.ModuleType(pkg))

_module("program.db.base_model", Base=Base)
_module("program.media.item", MediaItem=MediaItem, Movie=Movie)

ENGINE = create_engine("sqlite://")


class _SessionCtx:
    def __enter__(self):
        self.session = Session(ENGINE)
        return self.session

    def __exit__(self, *exc):
        self.session.close()
        return False


_module("program.db.db", db_session=lambda: _SessionCtx())


class RunnerResult:
    def __init__(self, media_items=None):
        self.media_items = media_items or []


_module("program.core.runner", RunnerResult=RunnerResult, MediaItemGenerator=object)


class BaseIndexer:
    def __init__(self):
        pass

    @staticmethod
    def copy_items(source, target):
        return target


_module("program.services.indexers.base", BaseIndexer=BaseIndexer)


class _Settings:
    class content:
        class brochure:
            enabled = True
            pages_per_listing = 1
            enrich_batch_size = 10
            refresh_interval = 43200
            enrich_interval = 600
            enrich_from_tpdb = True
            resolve_batch_size = 10
            resolve_interval = 300


_module("program.settings", settings_manager=types.SimpleNamespace(settings=_Settings))


# Titles the fake TPDB "knows". resolve_batch is exercised for real against
# this; only the network call is substituted, exactly as the matcher's own
# suite does.
TPDB_KNOWS = {"Pirates": "uuid-pirates"}
LOOKUPS: list[str] = []


def _fake_enrich_entry(entry):
    """Stands in for tpdb_lookup.enrich_entry, mirroring its write-back."""

    from datetime import datetime

    LOOKUPS.append(entry.title)

    if entry.tpdb_id or not entry.title:
        return False

    found = TPDB_KNOWS.get(entry.title)

    if not found:
        return False

    entry.tpdb_id = found
    entry.tpdb_kind = "movie"
    entry.match_state = "matched"
    entry.matched_at = datetime.now()

    return True


_module(
    "program.services.recommendations.tpdb_lookup",
    enrich_entry=_fake_enrich_entry,
)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


coll = _load("program.media.collection", SRC / "program" / "media" / "collection.py")
ae = _load(
    "program.services.recommendations.adultempire",
    SRC / "program" / "services" / "recommendations" / "adultempire.py",
)
indexer_mod = _load(
    "program.services.indexers.adultempire_indexer",
    SRC / "program" / "services" / "indexers" / "adultempire_indexer.py",
)
brochure_mod = _load(
    "program.services.recommendations.brochure",
    SRC / "program" / "services" / "recommendations" / "brochure.py",
)

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


def _reset():
    with Session(ENGINE) as s:
        s.query(coll.CollectionEntry).delete()
        s.query(coll.Collection).delete()
        s.query(MediaItem).delete()
        s.commit()


def _seed(**overrides):
    """One cached brochure entry, as sync_listings + enrich would leave it."""

    _reset()

    with Session(ENGINE) as s:
        collection = coll.Collection(
            key="adultempire-all-time-bestsellers",
            source="adultempire",
            name="All-Time Bestsellers",
        )
        s.add(collection)
        s.flush()

        fields = dict(
            collection_id=collection.id,
            external_source="adultempire",
            external_id="700215",
            title="Pirates",
            studio="Digital Playground",
            year=2005,
            rank=1,
            rating=4.69,
            duration_minutes=123,
            released_at=datetime(2005, 9, 26),
            performers=["Jesse Jane", "Carmen Luvana"],
            poster_path="https://img/700215h.jpg",
            match_state=coll.MATCH_SELF_SOURCED,
            category="all-time-bestsellers",
        )
        fields.update(overrides)
        s.add(coll.CollectionEntry(**fields))
        s.commit()


class FakeClient:
    """Stands in for the network. Records what was asked for."""

    def __init__(self, items=None, detail=None):
        self.items = items or []
        self.detail = detail
        self.listing_calls = []
        self.enrich_calls = []

    def listing(self, name, pages=1):
        self.listing_calls.append((name, pages))
        return [i for i in self.items if i.listing == name]

    def enrich(self, probe):
        self.enrich_calls.append(probe.url)

        if self.detail is None:
            return probe

        for attr, value in self.detail.items():
            setattr(probe, attr, value)

        return probe


def _service(client):
    service = brochure_mod.BrochureService()
    service.client = client
    return service


# ------------------------------------------------------- indexing without TPDB


def test_entry_is_actionable_without_a_tpdb_id():
    """The whole premise: a brochure title needs no TPDB record to be used."""

    _seed()

    with Session(ENGINE) as s:
        entry = s.execute(select(coll.CollectionEntry)).scalars().one()
        assert entry.tpdb_id is None
        assert entry.actionable, "self-sourced entry was not actionable"


def test_build_movie_uses_only_the_cached_entry():
    _seed()

    with Session(ENGINE) as s:
        entry = s.execute(select(coll.CollectionEntry)).scalars().one()
        movie = indexer_mod.build_movie(entry)

    assert movie.adultempire_id == "700215"
    assert movie.title == "Pirates"
    assert movie.site_name == "Digital Playground", movie.site_name
    assert movie.aired_at == datetime(2005, 9, 26), movie.aired_at
    assert movie.performers == ["Jesse Jane", "Carmen Luvana"]
    assert movie.tpdb_id is None, "indexing invented a TPDB id"


def test_built_movie_is_adult_so_it_scrapes_the_right_categories():
    """Without this the title is searched as a mainstream film and finds nothing."""

    _seed()

    with Session(ENGINE) as s:
        entry = s.execute(select(coll.CollectionEntry)).scalars().one()
        movie = indexer_mod.build_movie(entry)

    assert movie.is_adult, "brochure title would scrape as mainstream"


def test_year_only_entry_still_gets_an_aired_at():
    _seed(released_at=None)

    with Session(ENGINE) as s:
        entry = s.execute(select(coll.CollectionEntry)).scalars().one()
        movie = indexer_mod.build_movie(entry)

    assert movie.aired_at == datetime(2005, 1, 1), movie.aired_at


def test_indexer_yields_a_movie_for_a_stub_item():
    _seed()
    stub = MediaItem({"adultempire_id": "700215"})
    stub.type = "mediaitem"

    results = list(indexer_mod.AdultEmpireIndexer().run(stub))

    assert len(results) == 1, results
    assert results[0].media_items[0].title == "Pirates"


def test_indexer_skips_when_nothing_is_cached():
    _reset()
    stub = MediaItem({"adultempire_id": "999999"})
    stub.type = "mediaitem"

    assert list(indexer_mod.AdultEmpireIndexer().run(stub)) == []


def test_reindex_does_not_clobber_tpdb_data():
    """The brochure fills gaps; it is not the authority once TPDB has spoken."""

    _seed()
    movie = Movie({"adultempire_id": "700215", "title": "Better Title",
                   "site_name": "Real Site"})

    with Session(ENGINE) as s:
        entry = s.execute(select(coll.CollectionEntry)).scalars().one()
        indexer_mod.AdultEmpireIndexer._apply(movie, entry)

    assert movie.title == "Better Title", movie.title
    assert movie.site_name == "Real Site", movie.site_name
    assert movie.performers == ["Jesse Jane", "Carmen Luvana"], "gap not filled"


# ------------------------------------------------------------------ sync/cache


def _ranked(pid, title, rank, listing="all-time-bestsellers", poster="https://img/x.jpg"):
    return ae.RankedTitle(
        product_id=pid, title=title, rank=rank, listing=listing,
        url=f"/{pid}/", poster=poster,
    )


def test_sync_writes_entries_with_rank_and_poster():
    _reset()
    client = FakeClient([_ranked("1", "One", 1), _ranked("2", "Two", 2)])
    written = _service(client).sync_listings()

    assert written >= 2, written

    with Session(ENGINE) as s:
        rows = {e.external_id: e for e in s.query(coll.CollectionEntry).all()}

    assert rows["1"].rank == 1 and rows["2"].rank == 2
    assert rows["1"].match_state == coll.MATCH_SELF_SOURCED
    assert rows["1"].poster_path == "https://img/x.jpg"


def test_resync_updates_rank_without_duplicating():
    _reset()
    service = _service(FakeClient([_ranked("1", "One", 1)]))
    service.sync_listings()

    service.client = FakeClient([_ranked("1", "One", 7)])
    service.sync_listings()

    with Session(ENGINE) as s:
        rows = s.query(coll.CollectionEntry).filter_by(external_id="1").all()

    assert len(rows) == 1, f"duplicated on resync: {len(rows)}"
    assert rows[0].rank == 7, rows[0].rank


def test_dropped_title_is_removed_unless_requested():
    """A title that fell off the chart should not linger at a stale rank."""

    _reset()
    service = _service(FakeClient([_ranked("1", "One", 1), _ranked("2", "Two", 2)]))
    service.sync_listings()

    with Session(ENGINE) as s:
        item = MediaItem({"adultempire_id": "2"})
        s.add(item)
        s.flush()
        s.query(coll.CollectionEntry).filter_by(external_id="2").update(
            {"media_item_id": item.id}
        )
        s.commit()

    service.client = FakeClient([_ranked("1", "One", 1)])
    service.sync_listings()

    with Session(ENGINE) as s:
        remaining = {e.external_id for e in s.query(coll.CollectionEntry).all()}

    assert "1" in remaining
    assert "2" in remaining, "a requested title lost its provenance row"


def test_enrichment_uses_the_bare_id_url():
    """A wrong slug answers 200 with none of the product markup on it."""

    _reset()
    _service(FakeClient([_ranked("700215", "Pirates", 1)])).sync_listings()

    client = FakeClient(detail={"rating": 4.69, "studio": "Digital Playground",
                                "year": 2005, "released": "Sep 26 2005",
                                "duration_minutes": 123, "performers": ["Jesse Jane"]})
    _service(client).enrich_batch()

    assert client.enrich_calls == ["/700215/"], client.enrich_calls


def test_enrichment_fills_rating_and_cast():
    _reset()
    _service(FakeClient([_ranked("700215", "Pirates", 1)])).sync_listings()

    client = FakeClient(detail={"rating": 4.69, "studio": "Digital Playground",
                                "year": 2005, "released": "Sep 26 2005",
                                "duration_minutes": 123, "performers": ["Jesse Jane"]})
    done = _service(client).enrich_batch()

    assert done == 1, done

    with Session(ENGINE) as s:
        entry = s.query(coll.CollectionEntry).filter_by(external_id="700215").one()

    assert entry.rating == 4.69
    assert entry.studio == "Digital Playground"
    assert entry.duration_minutes == 123
    assert entry.released_at == datetime(2005, 9, 26), entry.released_at
    assert entry.performers == ["Jesse Jane"]


def test_enrichment_skips_already_enriched():
    """Resumable: rating is the marker, so a second run does no work."""

    _seed()
    client = FakeClient(detail={"rating": 1.0})
    done = _service(client).enrich_batch()

    assert done == 0 and client.enrich_calls == [], client.enrich_calls


def test_enrichment_pauses_on_outage():
    _reset()
    _service(FakeClient([_ranked("1", "One", 1), _ranked("2", "Two", 2)])).sync_listings()

    class Broken(FakeClient):
        def enrich(self, probe):
            raise ae.AdultEmpireError("503")

    assert _service(Broken()).enrich_batch() == 0

    with Session(ENGINE) as s:
        unrated = s.query(coll.CollectionEntry).filter(
            coll.CollectionEntry.rating.is_(None)).count()

    assert unrated == 2, f"{unrated} left unrated, expected both to survive"


# ------------------------------------------------- resolving against TPDB


def _resolve_service():
    """resolve_batch touches TPDB, never the storefront, so no client."""

    return _service(FakeClient())


def test_resolution_attaches_a_tpdb_id():
    _seed()
    LOOKUPS.clear()

    assert _resolve_service().resolve_batch() == 1

    with Session(ENGINE) as s:
        entry = s.query(coll.CollectionEntry).one()

        assert entry.tpdb_id == "uuid-pirates"
        assert entry.match_state == "matched"


def test_a_known_miss_is_never_asked_about_twice():
    """The bug this pass exists to avoid creating.

    One title in five has no TPDB record. Selecting on "tpdb_id is null"
    alone would hand those back on every run forever, so the whole rate limit
    goes on re-confirming misses while never-tried entries wait behind them.
    """

    _seed(title="Nurses", external_id="999001")
    LOOKUPS.clear()

    service = _resolve_service()

    assert service.resolve_batch() == 0
    assert LOOKUPS == ["Nurses"]

    # Second run: the entry still has no tpdb_id, and must still be skipped.
    assert service.resolve_batch() == 0
    assert LOOKUPS == ["Nurses"], f"asked TPDB again: {LOOKUPS}"


def test_a_miss_is_still_requestable_afterwards():
    """A failed lookup must not cost the user a title that works. The entry
    keeps its storefront metadata, and `actionable` is what proves it."""

    _seed(title="Nurses", external_id="999001")
    _resolve_service().resolve_batch()

    with Session(ENGINE) as s:
        entry = s.query(coll.CollectionEntry).one()

        assert entry.match_state == coll.MATCH_SELF_SOURCED
        assert entry.actionable is True
        assert entry.matched_at is not None


def test_a_resolved_entry_is_not_re_resolved():
    _seed()
    _resolve_service().resolve_batch()
    LOOKUPS.clear()

    assert _resolve_service().resolve_batch() == 0
    assert LOOKUPS == []


def test_resolution_can_be_switched_off():
    """It is additive: the titles are downloadable without it."""

    _seed()
    LOOKUPS.clear()
    _Settings.content.brochure.enrich_from_tpdb = False

    try:
        assert _resolve_service().resolve_batch() == 0
        assert LOOKUPS == []
    finally:
        _Settings.content.brochure.enrich_from_tpdb = True


for _name, _fn in sorted(list(globals().items())):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")

for _name, _err in FAILED:
    print(f"  FAIL {_name}: {_err}")

sys.exit(1 if FAILED else 0)
