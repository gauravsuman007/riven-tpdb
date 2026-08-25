"""User collections: adding titles, deduping, and the TPDB mirror.

Runs the real service against a real in-memory SQLite database; only the
things the service cannot bring up in a test process -- the FUSE-dependent
program package, the TPDB HTTP client, the settings manager -- are stubbed.

The behaviour worth protecting here is mostly negative: adding a title to a
collection must NOT request it, must NOT create a MediaItem, and must NOT fail
because TPDB was unreachable.
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
    from sqlalchemy import create_engine
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
    """Only the columns the collections service touches."""

    __tablename__ = "MediaItem"

    id: Mapped[int] = mapped_column(sqlalchemy.Integer, primary_key=True)
    title: Mapped[str] = mapped_column(sqlalchemy.String, nullable=True)
    year: Mapped[int] = mapped_column(sqlalchemy.Integer, nullable=True)
    site_name: Mapped[str] = mapped_column(sqlalchemy.String, nullable=True)
    poster_path: Mapped[str] = mapped_column(sqlalchemy.String, nullable=True)
    performers: Mapped[list] = mapped_column(sqlalchemy.JSON, nullable=True)
    tpdb_id: Mapped[str] = mapped_column(sqlalchemy.String, nullable=True)
    adultempire_id: Mapped[str] = mapped_column(sqlalchemy.String, nullable=True)


for pkg in ("program", "program.db", "program.media", "program.apis",
            "program.services", "program.services.awards",
            "program.services.collections", "program.services.recommendations",
            "program.settings", "program.utils"):
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
        class collections:
            sync_to_tpdb = False

    class tpdb:
        api_token = "token"


_module("program.settings", settings_manager=types.SimpleNamespace(settings=_Settings))

ENGINE = create_engine("sqlite://")


class _SessionCtx:
    def __enter__(self):
        self.session = Session(ENGINE)
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


_load("program.utils.text_matching", SRC / "program" / "utils" / "text_matching.py")
coll = _load("program.media.collection", SRC / "program" / "media" / "collection.py")
_load("program.services.awards.matching",
      SRC / "program" / "services" / "awards" / "matching.py")
# Before tpdb_lookup: it now owns the TPDB client lookup, so it imports kink
# at module level.
_module("kink", di={})

_load("program.services.recommendations.tpdb_lookup",
      SRC / "program" / "services" / "recommendations" / "tpdb_lookup.py")

service = _load("program.services.collections.service",
                SRC / "program" / "services" / "collections" / "service.py")

Base.metadata.create_all(ENGINE)


# ------------------------------------------------------------- fake TPDB data

class _Site:
    def __init__(self, name): self.name = name


class _Perf:
    def __init__(self, name): self.name = name


class _Record:
    def __init__(self, id, title, site=None, date=None, performers=(), poster=None):
        self.id = id
        self.title = title
        self.site = _Site(site) if site else None
        self.date = date
        self.performers = [_Perf(p) for p in performers]
        self.poster = poster
        self.posters = None

    def flat(self):
        """What a search endpoint actually returns: no site, no cast."""

        return _Record(self.id, self.title, date=self.date)


CATALOGUE = {
    "uuid-pirates": _Record(
        "uuid-pirates", "Pirates", "Digital Playground", "2005-09-26",
        ["Jesse Jane", "Janine Lindemulder"], "pirates.jpg",
    ),
}


class FakeApi(TpdbApi):
    def __init__(self):
        self.searches = []
        self.collected = set()
        self.added = []
        self.fail = False

    def search_movies_text(self, title, per_page=20):
        self.searches.append(title)

        if self.fail:
            raise TpdbApiError("TPDB is down", status_code=503)

        return [
            record.flat()
            for record in CATALOGUE.values()
            if title.lower() in (record.title or "").lower()
        ]

    def get_movie(self, movie_id):
        return CATALOGUE.get(movie_id)

    def get_scene(self, scene_id):
        return None

    def numeric_id(self, uuid, kind="movie"):
        return 4242 if uuid in CATALOGUE else None

    def is_collected(self, numeric_id):
        return numeric_id in self.collected

    def add_to_collection(self, numeric_id):
        self.added.append(numeric_id)
        self.collected.add(numeric_id)
        return True


API = FakeApi()
# Mutated in place, never reassigned: the service did `from kink import di`, so
# it holds a reference to this exact dict and a rebind here would be invisible
# to it -- which reads as "TPDB is unconfigured" rather than as a broken test.
sys.modules["kink"].di[TpdbApi] = API


# ------------------------------------------------------------------- fixtures

def _reset():
    global API
    API = FakeApi()
    sys.modules["kink"].di[TpdbApi] = API
    _Settings.content.collections.sync_to_tpdb = False

    with Session(ENGINE) as session:
        session.query(coll.CollectionEntry).delete()
        session.query(coll.Collection).delete()
        session.query(MediaItem).delete()
        session.commit()


def _brochure_entry(session, **overrides):
    """A catalogue row exactly as the brochure sync writes one."""

    collection = coll.Collection(
        key="adultempire-bestsellers", source="adultempire", name="Bestsellers"
    )
    session.add(collection)
    session.flush()

    payload = {
        "collection_id": collection.id,
        "title": "Pirates",
        "studio": "Digital Playground",
        "performers": ["Jesse Jane", "Janine Lindemulder"],
        "year": 2005,
        "released_at": datetime(2005, 9, 26),
        "external_source": "adultempire",
        "external_id": "700215",
        "rank": 1,
        "match_state": coll.MATCH_SELF_SOURCED,
    }
    payload.update(overrides)

    entry = coll.CollectionEntry(**payload)
    session.add(entry)
    session.flush()

    return entry


PASSED = []
FAILED = []


def check(name, fn):
    _reset()

    try:
        fn()
        PASSED.append(name)
    except Exception as exc:
        FAILED.append((name, exc))


# ---------------------------------------------------------------------- tests

def test_slugify_strips_punctuation():
    assert service.slugify("Jesse Jane's Best!") == "jesse-jane-s-best"


def test_slugify_never_returns_empty():
    # A name of pure punctuation would otherwise produce the key "user-",
    # which every other unnameable collection would then collide with.
    assert service.slugify("!!!") != ""


def test_duplicate_names_get_distinct_keys():
    with Session(ENGINE) as session:
        first = service.create(session, "Favourites")
        second = service.create(session, "Favourites")
        session.commit()

        assert first.key == "user-favourites", first.key
        assert second.key == "user-favourites-2", second.key


def test_created_collection_is_user_sourced():
    with Session(ENGINE) as session:
        collection = service.create(session, "Favourites")
        session.commit()

        assert collection.source == "user"


def test_create_rejects_blank_name():
    with Session(ENGINE) as session:
        try:
            service.create(session, "   ")
        except service.CollectionError:
            return

    raise AssertionError("a blank name was accepted")


def test_add_tpdb_title_pulls_metadata():
    with Session(ENGINE) as session:
        collection = service.create(session, "Favourites")
        entry = service.add_tpdb_title(session, collection, "uuid-pirates")
        session.commit()

        assert entry.title == "Pirates", entry.title
        assert entry.studio == "Digital Playground", entry.studio
        assert entry.year == 2005, entry.year
        assert entry.poster_path == "pirates.jpg"
        assert entry.match_state == coll.MATCH_MATCHED


def test_adding_does_not_create_a_media_item():
    """The whole point of the model: a collection is not the library."""

    with Session(ENGINE) as session:
        collection = service.create(session, "Favourites")
        service.add_tpdb_title(session, collection, "uuid-pirates")
        session.commit()

        assert session.query(MediaItem).count() == 0
        assert session.query(coll.CollectionEntry).one().media_item_id is None


def test_add_tpdb_title_is_idempotent():
    with Session(ENGINE) as session:
        collection = service.create(session, "Favourites")
        first = service.add_tpdb_title(session, collection, "uuid-pirates")
        second = service.add_tpdb_title(session, collection, "uuid-pirates")
        session.commit()

        assert first.id == second.id
        assert session.query(coll.CollectionEntry).count() == 1


def test_add_adopts_an_existing_library_item():
    with Session(ENGINE) as session:
        session.add(MediaItem(id=1, title="Pirates", tpdb_id="uuid-pirates"))
        session.flush()

        collection = service.create(session, "Favourites")
        entry = service.add_tpdb_title(session, collection, "uuid-pirates")
        session.commit()

        assert entry.media_item_id == 1, entry.media_item_id


def test_adult_empire_entry_resolves_tpdb_on_add():
    """The behaviour the brochure page's add button depends on."""

    with Session(ENGINE) as session:
        source_entry = _brochure_entry(session)
        collection = service.create(session, "Favourites")
        entry = service.add_catalogue_entry(session, collection, source_entry)
        session.commit()

        assert entry.tpdb_id == "uuid-pirates", entry.tpdb_id
        assert entry.match_state == coll.MATCH_MATCHED
        assert entry.poster_path == "pirates.jpg"
        # The source id is kept: it is what the scrapers and the request path
        # address the title by until the library item gains a TPDB id.
        assert entry.external_id == "700215"


def test_adding_leaves_the_source_entry_untouched():
    with Session(ENGINE) as session:
        source_entry = _brochure_entry(session)
        collection = service.create(session, "Favourites")
        service.add_catalogue_entry(session, collection, source_entry)
        session.commit()

        refreshed = session.get(coll.CollectionEntry, source_entry.id)

        assert refreshed.collection_id != collection.id
        assert refreshed.tpdb_id is None, (
            "the brochure row was mutated; a re-sync would fight with it"
        )


def test_unmatched_adult_empire_entry_is_still_added():
    """TPDB missing must not block the add -- the title is usable without it."""

    with Session(ENGINE) as session:
        source_entry = _brochure_entry(session, title="Nothing TPDB Knows")
        collection = service.create(session, "Favourites")
        entry = service.add_catalogue_entry(session, collection, source_entry)
        session.commit()

        assert entry.id is not None
        assert entry.tpdb_id is None
        assert entry.match_state == coll.MATCH_SELF_SOURCED
        assert entry.actionable, "a self-sourced entry must stay requestable"


def test_tpdb_outage_does_not_fail_the_add():
    API.fail = True

    with Session(ENGINE) as session:
        source_entry = _brochure_entry(session)
        collection = service.create(session, "Favourites")
        entry = service.add_catalogue_entry(session, collection, source_entry)
        session.commit()

        assert entry.id is not None
        assert entry.tpdb_id is None


def test_add_library_item_links_and_copies():
    with Session(ENGINE) as session:
        session.add(
            MediaItem(
                id=7,
                title="Pirates",
                year=2005,
                site_name="Digital Playground",
                tpdb_id="uuid-pirates",
                poster_path="local.jpg",
                performers=["Jesse Jane"],
            )
        )
        session.flush()

        collection = service.create(session, "Favourites")
        entry = service.add_library_item(session, collection, 7)
        session.commit()

        assert entry.media_item_id == 7
        assert entry.studio == "Digital Playground"
        assert entry.performers == ["Jesse Jane"]


def test_add_library_item_rejects_unknown_id():
    with Session(ENGINE) as session:
        collection = service.create(session, "Favourites")

        try:
            service.add_library_item(session, collection, 999)
        except service.CollectionError:
            return

    raise AssertionError("an unknown library id was accepted")


def test_tpdb_mirror_is_off_by_default():
    with Session(ENGINE) as session:
        collection = service.create(session, "Favourites")
        service.add_tpdb_title(session, collection, "uuid-pirates")
        session.commit()

    assert API.added == [], "titles were pushed to TPDB without being asked"


def test_tpdb_mirror_adds_when_enabled():
    _Settings.content.collections.sync_to_tpdb = True

    with Session(ENGINE) as session:
        collection = service.create(session, "Favourites")
        service.add_tpdb_title(session, collection, "uuid-pirates")
        session.commit()

    assert API.added == [4242], API.added


def test_tpdb_mirror_skips_titles_already_collected():
    _Settings.content.collections.sync_to_tpdb = True
    API.collected.add(4242)

    with Session(ENGINE) as session:
        collection = service.create(session, "Favourites")
        service.add_tpdb_title(session, collection, "uuid-pirates")
        session.commit()

    assert API.added == [], "a title already in the TPDB collection was re-posted"


def test_tpdb_mirror_failure_never_breaks_the_add():
    _Settings.content.collections.sync_to_tpdb = True

    def explode(numeric_id):
        raise TpdbApiError("boom", status_code=500)

    API.add_to_collection = explode

    with Session(ENGINE) as session:
        collection = service.create(session, "Favourites")
        entry = service.add_tpdb_title(session, collection, "uuid-pirates")
        session.commit()

        assert entry.id is not None


for _name, _fn in sorted(list(globals().items())):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")

for _name, _err in FAILED:
    print(f"  FAIL {_name}: {_err}")

sys.exit(1 if FAILED else 0)
