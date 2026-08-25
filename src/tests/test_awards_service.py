"""Awards service: resolution ordering, state transitions and auto-requests.

Needs SQLAlchemy (it runs against a real in-memory database); skips cleanly
without it. Everything else the service imports -- kink, the TPDB client, the
settings manager, the event manager -- is stubbed here, so the FUSE-dependent
program package is never imported.
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
except ImportError:  # pragma: no cover - environment without the app deps
    print("SKIP: sqlalchemy not installed")
    sys.exit(0)


# ------------------------------------------------------------------- stubbing

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
    """Minimal stand-in: the service only ever sets tpdb_id and requested_by."""

    __tablename__ = "MediaItem"

    id: Mapped[int] = mapped_column(sqlalchemy.Integer, primary_key=True)
    tpdb_id: Mapped[str] = mapped_column(sqlalchemy.String, nullable=True)
    requested_by: Mapped[str] = mapped_column(sqlalchemy.String, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(sqlalchemy.DateTime, nullable=True)

    def __init__(self, payload=None, **kwargs):
        super().__init__(**(payload or kwargs))


for pkg in ("program", "program.db", "program.media", "program.apis",
            "program.services", "program.services.awards", "program.settings",
            "program.utils"):
    sys.modules.setdefault(pkg, types.ModuleType(pkg))

_module("program.db.base_model", Base=Base)
_module("program.media.item", MediaItem=MediaItem)


class TpdbApiError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class TpdbApi:
    pass


_module("program.apis.tpdb_api", TpdbApi=TpdbApi, TpdbApiError=TpdbApiError)


class _Settings:
    class content:
        class awards:
            enabled = True
            include_nominees = False
            auto_request_winners = True
            first_year = 1987
            resolve_batch_size = 40
            refresh_interval = 604800
            resolve_interval = 300

    class tpdb:
        api_token = "token"


_module("program.settings", settings_manager=types.SimpleNamespace(settings=_Settings))

ENGINE = create_engine("sqlite://")
SESSIONS = []


class _SessionCtx:
    def __enter__(self):
        self.session = Session(ENGINE)
        SESSIONS.append(self.session)
        return self.session

    def __exit__(self, *exc):
        self.session.close()
        return False


_module("program.db.db", db_session=lambda: _SessionCtx())


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


AWARDS = SRC / "program" / "services" / "awards"
_load("program.utils.text_matching", SRC / "program" / "utils" / "text_matching.py")
coll = _load("program.media.collection", SRC / "program" / "media" / "collection.py")
_load("program.services.awards.wikitable", AWARDS / "wikitable.py")
_load("program.services.awards.avn", AWARDS / "avn.py")
_load("program.services.awards.matching", AWARDS / "matching.py")

QUEUED = []


class _EventManager:
    def add_item(self, item):
        QUEUED.append(item)
        return True


_module("kink", di={})
_module("program.program", Program=object)
sys.modules["kink"].di = {
    sys.modules["program.program"].Program: types.SimpleNamespace(em=_EventManager()),
    TpdbApi: TpdbApi(),
}

service_mod = _load("program.services.awards.service", AWARDS / "service.py")

Base.metadata.create_all(ENGINE)


# ------------------------------------------------------------- fake TPDB data

class _Img:
    def __init__(self, large): self.large = large


class _Site:
    def __init__(self, name): self.name = name


class _Perf:
    def __init__(self, name): self.name = name


class _Record:
    """A TPDB detail record. Search results deliberately omit site/performers."""

    def __init__(self, id, title, site=None, date=None, performers=(), poster=None):
        self.id = id
        self.title = title
        self.site = _Site(site) if site else None
        self.date = date
        self.performers = [_Perf(p) for p in performers]
        self.poster = poster
        self.posters = _Img(poster) if poster else None

    def flat(self):
        """The shape a search endpoint actually returns: no site, no cast."""

        return _Record(self.id, self.title, date=self.date)


CATALOGUE = {
    "uuid-strip": _Record("uuid-strip", "Strip", "Dorcel", "2025-06-01",
                          ["Tommy Pistol"], "poster-strip.jpg"),
    "uuid-other": _Record("uuid-other", "Strip", "Unrelated Studio", "2011-01-01"),
    "uuid-savages3": _Record("uuid-savages3", "Anal Savages 3", "Jules Jordan Video",
                             "2020-01-01"),
}


class FakeApi:
    def __init__(self):
        self.searches = 0
        self.details = 0
        self.fail_with = None

    def search_movies_text(self, query, per_page=None):
        if self.fail_with:
            raise self.fail_with

        self.searches += 1
        return [r.flat() for r in CATALOGUE.values()
                if query.split()[0].lower() in (r.title or "").lower()]

    def search_scenes_text(self, query, per_page=None):
        if self.fail_with:
            raise self.fail_with

        self.searches += 1
        return []

    def get_movie(self, movie_id):
        self.details += 1
        return CATALOGUE.get(movie_id)

    def get_scene(self, scene_id):
        self.details += 1
        return None


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


def _fresh_service(**entries_by_kind):
    """A service over a database seeded with the given entries."""

    with Session(ENGINE) as s:
        s.query(coll.CollectionEntry).delete()
        s.query(coll.Collection).delete()
        s.query(MediaItem).delete()
        s.commit()

        c = coll.Collection(key="avn-2026", source="avn", name="AVN Awards 2026",
                            year=2026)
        s.add(c)
        s.flush()

        for title, kwargs in entries_by_kind.items():
            s.add(coll.CollectionEntry(collection_id=c.id, title=kwargs.pop("title", title),
                                       year=2026, **kwargs))
        s.commit()

    QUEUED.clear()
    service = service_mod.AwardsService()
    service.api = FakeApi()
    return service


def test_winner_resolves_via_detail_lookup():
    """Search is flat, so the studio signal can only come from the detail call."""

    svc = _fresh_service(strip={"title": "Strip", "studio": "Dorcel/Pulse",
                                "performers": ["Tommy Pistol"], "winner": True,
                                "category": "Best Feature"})
    matched, unmatched = svc.resolve_batch()

    assert (matched, unmatched) == (1, 0), f"{matched=} {unmatched=}"
    assert svc.api.details >= 1, "no detail lookup was made"

    with Session(ENGINE) as s:
        e = s.execute(select(coll.CollectionEntry)).scalars().one()
        assert e.tpdb_id == "uuid-strip", e.tpdb_id
        assert e.match_state == coll.MATCH_MATCHED
        assert e.poster_path == "poster-strip.jpg", e.poster_path


def test_wrong_studio_does_not_match():
    svc = _fresh_service(x={"title": "Strip", "studio": "Nonexistent Studio",
                            "performers": ["Nobody At All"], "winner": True,
                            "category": "Best Feature"})
    CATALOGUE.pop("uuid-strip")

    try:
        matched, unmatched = svc.resolve_batch()
    finally:
        CATALOGUE["uuid-strip"] = _Record("uuid-strip", "Strip", "Dorcel",
                                          "2025-06-01", ["Tommy Pistol"],
                                          "poster-strip.jpg")

    assert (matched, unmatched) == (0, 1), f"{matched=} {unmatched=}"

    with Session(ENGINE) as s:
        e = s.execute(select(coll.CollectionEntry)).scalars().one()
        assert e.match_state == coll.MATCH_UNMATCHED
        assert e.tpdb_id is None


def test_unmatched_entry_is_kept_not_deleted():
    """An unresolved award title stays visible as a known gap."""

    svc = _fresh_service(x={"title": "Completely Unknown Film", "winner": True,
                            "category": "Best Film"})
    svc.resolve_batch()

    with Session(ENGINE) as s:
        assert s.query(coll.CollectionEntry).count() == 1


def test_winners_resolve_before_nominees():
    svc = _fresh_service(
        nominee={"title": "Strip", "studio": "Dorcel", "winner": False,
                 "category": "Best Anal Movie"},
        winner={"title": "Strip", "studio": "Dorcel", "winner": True,
                "category": "Best Feature"},
    )
    svc.resolve_batch(limit=1)

    with Session(ENGINE) as s:
        done = s.execute(
            select(coll.CollectionEntry).where(
                coll.CollectionEntry.match_state != coll.MATCH_PENDING)
        ).scalars().all()
        assert len(done) == 1 and done[0].winner, [(d.title, d.winner) for d in done]


def test_api_outage_pauses_instead_of_burning_the_queue():
    """A TPDB outage must not mark the whole backlog unmatched."""

    svc = _fresh_service(
        a={"title": "Strip", "winner": True, "category": "Best Feature"},
        b={"title": "Strip", "winner": True, "category": "Best Anal Movie"},
    )
    svc.api.fail_with = TpdbApiError("503", status_code=503)
    matched, unmatched = svc.resolve_batch()

    assert (matched, unmatched) == (0, 0), f"{matched=} {unmatched=}"

    with Session(ENGINE) as s:
        pending = s.query(coll.CollectionEntry).filter(
            coll.CollectionEntry.match_state == coll.MATCH_PENDING).count()
        assert pending == 2, f"{pending} still pending, expected 2"


def test_outage_partway_keeps_earlier_results():
    """Per-entry commits: work done before an outage survives it."""

    svc = _fresh_service(
        a={"title": "Strip", "studio": "Dorcel", "performers": ["Tommy Pistol"],
           "winner": True, "category": "Best Feature"},
        b={"title": "Strip", "studio": "Dorcel", "performers": ["Tommy Pistol"],
           "winner": True, "category": "Best Anal Movie"},
    )

    real_search = svc.api.search_movies_text
    calls = {"n": 0}

    def flaky(query, per_page=None):
        calls["n"] += 1

        if calls["n"] > 1:
            raise TpdbApiError("503", status_code=503)

        return real_search(query, per_page)

    svc.api.search_movies_text = flaky
    matched, _ = svc.resolve_batch()

    assert matched == 1, f"matched {matched}"

    with Session(ENGINE) as s:
        done = s.query(coll.CollectionEntry).filter(
            coll.CollectionEntry.match_state == coll.MATCH_MATCHED).count()
        pending = s.query(coll.CollectionEntry).filter(
            coll.CollectionEntry.match_state == coll.MATCH_PENDING).count()

    assert done == 1, f"the committed result was lost ({done} matched)"
    assert pending == 1, f"{pending} pending, expected the outage entry to remain"


def test_only_winners_are_auto_requested():
    svc = _fresh_service(
        w={"title": "Strip", "studio": "Dorcel", "performers": ["Tommy Pistol"],
           "winner": True, "category": "Best Feature"},
        n={"title": "Strip", "studio": "Dorcel", "performers": ["Tommy Pistol"],
           "winner": False, "category": "Best Anal Movie"},
    )
    svc.resolve_batch()
    queued = svc.request_matched_winners()

    assert queued == 1, f"queued {queued}"
    assert len(QUEUED) == 1
    assert QUEUED[0].tpdb_id == "uuid-strip"
    assert QUEUED[0].requested_by == "awards"

    with Session(ENGINE) as s:
        nominee = s.execute(
            select(coll.CollectionEntry).where(coll.CollectionEntry.winner.is_(False))
        ).scalars().one()
        assert nominee.media_item_id is None, "a nominee was requested"


def test_auto_request_is_off_when_disabled():
    svc = _fresh_service(w={"title": "Strip", "studio": "Dorcel", "winner": True,
                            "category": "Best Feature"})
    svc.resolve_batch()
    svc.settings.auto_request_winners = False

    try:
        assert svc.request_matched_winners() == 0
        assert not QUEUED
    finally:
        svc.settings.auto_request_winners = True


def test_existing_library_item_is_adopted_not_duplicated():
    svc = _fresh_service(w={"title": "Strip", "studio": "Dorcel",
                            "performers": ["Tommy Pistol"], "winner": True,
                            "category": "Best Feature"})
    svc.resolve_batch()

    with Session(ENGINE) as s:
        s.add(MediaItem({"tpdb_id": "uuid-strip", "requested_by": "someone-else"}))
        s.commit()

    queued = svc.request_matched_winners()

    assert queued == 0, "requested a duplicate"

    with Session(ENGINE) as s:
        e = s.execute(select(coll.CollectionEntry)).scalars().one()
        assert e.media_item_id is not None, "existing item was not adopted"
        assert s.query(MediaItem).count() == 1


def test_resolution_is_resumable():
    """State lives in the rows, so a second run continues rather than repeats."""

    svc = _fresh_service(
        a={"title": "Strip", "studio": "Dorcel", "winner": True, "category": "Best Feature"},
        b={"title": "Strip", "studio": "Dorcel", "winner": True, "category": "Best Anal Movie"},
        c={"title": "Strip", "studio": "Dorcel", "winner": True, "category": "Best Gonzo Movie"},
    )
    svc.resolve_batch(limit=2)
    after_first = svc.api.searches

    svc.resolve_batch(limit=2)

    with Session(ENGINE) as s:
        pending = s.query(coll.CollectionEntry).filter(
            coll.CollectionEntry.match_state == coll.MATCH_PENDING).count()
        assert pending == 0, f"{pending} left pending"

    # Only the third entry was searched on the second run.
    assert svc.api.searches == after_first + 1, (
        f"re-searched resolved entries: {after_first} -> {svc.api.searches}")


def test_sync_stores_winners_only_by_default():
    """Nominees must not reach the database at all, not merely be hidden."""

    svc = _fresh_service()
    svc.settings.include_nominees = False

    entry = lambda title, winner: types.SimpleNamespace(
        ceremony=43, year=2026, category="Best Feature", winner=winner,
        raw=title, title=title, studio="Dorcel", performers=[],
        is_media=True)

    fake_corpus = [entry("A Winner", True), entry("A Nominee", False)]
    real_build = sys.modules["program.services.awards.avn"].build_corpus
    sys.modules["program.services.awards.avn"].build_corpus = lambda *a, **k: fake_corpus

    try:
        svc.sync_corpus()
    finally:
        sys.modules["program.services.awards.avn"].build_corpus = real_build

    with Session(ENGINE) as s:
        titles = {e.title for e in s.query(coll.CollectionEntry).all()}

    assert titles == {"A Winner"}, f"nominees leaked into the database: {titles}"


def test_turning_nominees_off_prunes_stored_ones():
    """Changing the setting has to clean up what an earlier sync stored."""

    svc = _fresh_service(
        w={"title": "A Winner", "winner": True, "category": "Best Feature"},
        n={"title": "A Nominee", "winner": False, "category": "Best Feature"},
    )
    svc.settings.include_nominees = False

    real_build = sys.modules["program.services.awards.avn"].build_corpus
    sys.modules["program.services.awards.avn"].build_corpus = lambda *a, **k: [
        types.SimpleNamespace(ceremony=43, year=2026, category="Best Feature",
                              winner=True, raw="A Winner", title="A Winner",
                              studio=None, performers=[], is_media=True)
    ]

    try:
        svc.sync_corpus()
    finally:
        sys.modules["program.services.awards.avn"].build_corpus = real_build

    with Session(ENGINE) as s:
        titles = {e.title for e in s.query(coll.CollectionEntry).all()}

    assert titles == {"A Winner"}, f"stored nominee survived: {titles}"


def test_prune_spares_a_nominee_already_in_the_library():
    """Deleting it would make an owned title look like it arrived from nowhere."""

    svc = _fresh_service(
        n={"title": "A Nominee", "winner": False, "category": "Best Feature"},
    )
    svc.settings.include_nominees = False

    with Session(ENGINE) as s:
        item = MediaItem({"tpdb_id": "uuid-x", "requested_by": "user"})
        s.add(item)
        s.flush()
        s.query(coll.CollectionEntry).update({"media_item_id": item.id})
        s.commit()

    real_build = sys.modules["program.services.awards.avn"].build_corpus
    sys.modules["program.services.awards.avn"].build_corpus = lambda *a, **k: []

    try:
        svc.sync_corpus()
    finally:
        sys.modules["program.services.awards.avn"].build_corpus = real_build

    with Session(ENGINE) as s:
        assert s.query(coll.CollectionEntry).count() == 1, "requested nominee was pruned"


def test_nominees_stored_when_explicitly_enabled():
    svc = _fresh_service()
    svc.settings.include_nominees = True

    entry = lambda title, winner: types.SimpleNamespace(
        ceremony=43, year=2026, category="Best Feature", winner=winner,
        raw=title, title=title, studio=None, performers=[], is_media=True)

    real_build = sys.modules["program.services.awards.avn"].build_corpus
    sys.modules["program.services.awards.avn"].build_corpus = lambda *a, **k: [
        entry("A Winner", True), entry("A Nominee", False)]

    try:
        svc.sync_corpus()
    finally:
        sys.modules["program.services.awards.avn"].build_corpus = real_build
        svc.settings.include_nominees = False

    with Session(ENGINE) as s:
        titles = {e.title for e in s.query(coll.CollectionEntry).all()}

    assert titles == {"A Winner", "A Nominee"}, titles


def test_resolver_ignores_other_sources():
    """A self-sourced catalogue owes TPDB nothing and must not be resolved here."""

    _fresh_service(a={"title": "Strip", "studio": "Dorcel", "winner": True,
                      "category": "Best Feature"})

    with Session(ENGINE) as s:
        other = coll.Collection(key="adultempire-trending", source="adultempire",
                                name="Trending")
        s.add(other)
        s.flush()
        s.add(coll.CollectionEntry(collection_id=other.id, title="Some Title",
                                   external_source="adultempire", external_id="1",
                                   match_state=coll.MATCH_PENDING))
        s.commit()

    svc = service_mod.AwardsService()
    svc.api = FakeApi()
    svc.resolve_batch(limit=50)

    with Session(ENGINE) as s:
        brochure_entry = s.query(coll.CollectionEntry).filter_by(
            external_source="adultempire").one()

    assert brochure_entry.match_state == coll.MATCH_PENDING, (
        "the awards resolver spent TPDB calls on another source's entries")


def test_progress_counts_by_state():
    svc = _fresh_service(
        a={"title": "Strip", "studio": "Dorcel", "winner": True, "category": "Best Feature"},
        b={"title": "Nothing Like It", "winner": True, "category": "Best Anal Movie"},
    )
    svc.resolve_batch()
    progress = svc.progress()

    assert progress.get(coll.MATCH_MATCHED) == 1, progress
    assert progress.get(coll.MATCH_UNMATCHED) == 1, progress


def test_sync_prunes_stored_person_awards():
    """The category gates were tightened after corpora had already synced.

    ``sync_corpus`` only ever adds, so without an explicit prune a library that
    synced before the change would show Best Actor and Best Male Newcomer
    forever -- the rows are already there and nothing would revisit them.
    """

    svc = _fresh_service(
        film={"title": "A Real Film", "winner": True, "category": "Best Feature"},
        actor={"title": "A Real Film", "winner": True, "category": "Best Actor"},
        starlet={"title": "Another Film", "winner": True, "category": "Best New Starlet"},
        website={"title": "Brazzers", "winner": True, "category": "Best Web Site"},
    )

    real_build = sys.modules["program.services.awards.avn"].build_corpus
    # Non-empty on purpose: sync_corpus returns early on an empty corpus so
    # that a Wikipedia outage cannot delete the collections.
    sys.modules["program.services.awards.avn"].build_corpus = lambda *a, **k: [
        types.SimpleNamespace(ceremony=43, year=2026, category="Best Feature",
                              winner=True, raw="A Real Film", title="A Real Film",
                              studio=None, performers=[], is_media=True)
    ]

    try:
        svc.sync_corpus()
    finally:
        sys.modules["program.services.awards.avn"].build_corpus = real_build

    with Session(ENGINE) as s:
        categories = {e.category for e in s.query(coll.CollectionEntry).all()}

    assert categories == {"Best Feature"}, categories


def test_person_award_prune_removes_even_a_requested_title():
    """Deliberately unlike the nominee prune.

    Deleting the entry does not touch the MediaItem -- the film stays in the
    library exactly as it was, and only its listing on the awards page goes.
    Sparing requested entries would defeat the prune on precisely the
    ceremonies that have been synced longest, which are the ones full of
    auto-requested Best Actor winners.
    """

    svc = _fresh_service(
        actor={"title": "A Real Film", "winner": True, "category": "Best Actor"},
    )

    with Session(ENGINE) as s:
        item = MediaItem({"tpdb_id": "uuid-y", "requested_by": "user"})
        s.add(item)
        s.flush()
        s.query(coll.CollectionEntry).update({"media_item_id": item.id})
        s.commit()

    real_build = sys.modules["program.services.awards.avn"].build_corpus
    sys.modules["program.services.awards.avn"].build_corpus = lambda *a, **k: [
        types.SimpleNamespace(ceremony=43, year=2026, category="Best Feature",
                              winner=True, raw="A Real Film", title="A Real Film",
                              studio=None, performers=[], is_media=True)
    ]

    try:
        svc.sync_corpus()
    finally:
        sys.modules["program.services.awards.avn"].build_corpus = real_build

    with Session(ENGINE) as s:
        surviving = {e.category for e in s.query(coll.CollectionEntry).all()}
        # The library item is untouched; only the awards-page row went.
        assert s.query(MediaItem).count() == 1

    assert surviving == {"Best Feature"}, surviving


for _name, _fn in sorted(list(globals().items())):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")

for _name, _err in FAILED:
    print(f"  FAIL {_name}: {_err}")

sys.exit(1 if FAILED else 0)
