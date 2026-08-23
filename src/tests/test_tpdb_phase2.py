"""Tests for the TPDB (ThePornDB) integration (Phase 2).

Covers three layers, independently:

1. ``tpdb_mapping`` — pure dict -> Movie-dict mapping (stdlib only).
2. ``tpdb_api`` — Pydantic models + response parsing + client wiring.
3. ``tpdb_indexer`` — the indexer's ``run()`` control flow, using lightweight
   stubs for the framework pieces (DI, BaseIndexer, MediaItem/Movie).

The fixture payloads mirror ThePornDB's real REST contract (field names taken
from the official ThePornDatabase/Jellyfin.Plugin.ThePornDB models). Live TPDB
calls require an API token (``TPDB_API_TOKEN``) and are skipped when absent.
"""

import importlib.util
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Generator, TypeVar

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

# ---------------------------------------------------------------------------
# Fixtures (real TPDB JSON shape; fictional names)
# ---------------------------------------------------------------------------

SCENE_FIXTURE = {
    "id": "8f3b2c1d-0000-1111-2222-333344445555",
    "_id": 1001,
    "title": "Example Scene Title",
    "description": "A scene overview.",
    "rating": 8.4,
    "trailer": "https://cdn.example/trailer.mp4",
    "date": "2024-05-01",
    "duration": 1800,
    "site": {
        "id": 42,
        "uuid": "site-uuid-123",
        "name": "Example Studios",
        "parent": {"id": 7, "name": "Example Network"},
    },
    "performers": [
        {"id": "p-uuid-1", "name": "Performer One", "extras": {"gender": "female"}},
        {"id": "p-uuid-2", "name": "Performer Two"},
    ],
    "directors": [{"id": 13122, "name": "Director One"}],
    "tags": [
        {"id": 1, "name": "Big Tits"},
        {"id": 2, "name": "Romance"},
    ],
    "poster": "https://cdn.example/poster.jpg",
    "posters": {
        "full": "https://cdn.example/full.jpg",
        "large": "https://cdn.example/large.jpg",
    },
    "background": {
        "full": "https://cdn.example/bg-full.jpg",
        "large": "https://cdn.example/bg-large.jpg",
    },
}

MOVIE_FIXTURE = {
    "id": "movie-uuid-999",
    "title": "Example Full Movie",
    "description": "A full-length movie.",
    "rating": 7.2,
    "date": "2023-11-20",
    "site": {"id": 42, "uuid": "site-uuid-123", "name": "Example Studios"},
    "performers": [{"id": "p-uuid-1", "name": "Performer One"}],
    "directors": [{"id": 13122, "name": "Director One"}],
    "tags": [{"id": 3, "name": "Feature"}],
    "poster": "https://cdn.example/movie-poster.jpg",
}


def _load_real(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _stub(name: str, **attrs) -> ModuleType:
    mod = ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------

PASSED = []
FAILED = []


def check(name, fn):
    try:
        fn()
    except AssertionError as exc:
        FAILED.append((name, str(exc)))
    except Exception as exc:  # noqa: BLE001 - surface anything in tests
        FAILED.append((name, f"{type(exc).__name__}: {exc}"))
    else:
        PASSED.append(name)


def approx_datetime(year, month, day):
    return datetime(year, month, day)


# ---------------------------------------------------------------------------
# 1. Mapping tests
# ---------------------------------------------------------------------------

mapping = _load_real("tpdb_mapping_real", SRC / "program" / "services" / "indexers" / "tpdb_mapping.py")


def _test_map_full_scene():
    out = mapping.scene_to_movie_dict(SCENE_FIXTURE)
    assert out["title"] == "Example Scene Title"
    assert out["poster_path"] == "https://cdn.example/large.jpg"
    assert out["year"] == 2024
    assert out["tpdb_id"] == "8f3b2c1d-0000-1111-2222-333344445555"
    assert out["site_id"] == "site-uuid-123"
    assert out["site_name"] == "Example Studios"
    assert out["performers"] == ["Performer One", "Performer Two"]
    assert out["genres"] == ["big tits", "romance"]
    assert out["aired_at"] == approx_datetime(2024, 5, 1)
    assert out["rating"] == 8.4
    assert out["content_rating"] is None


def _test_map_movie():
    out = mapping.movie_to_movie_dict(MOVIE_FIXTURE)
    assert out["title"] == "Example Full Movie"
    assert out["tpdb_id"] == "movie-uuid-999"


def _test_map_site_id_fallback_to_int():
    scene = dict(SCENE_FIXTURE, site={"id": 42, "name": "X"})
    out = mapping.scene_to_movie_dict(scene)
    assert out["site_id"] == "42"
    assert out["site_name"] == "X"


def _test_map_no_site():
    scene = dict(SCENE_FIXTURE, site=None)
    out = mapping.scene_to_movie_dict(scene)
    assert out["site_id"] is None
    assert out["site_name"] is None


def _test_map_empty_lists_become_none():
    scene = dict(SCENE_FIXTURE, performers=[], tags=[])
    out = mapping.scene_to_movie_dict(scene)
    assert out["performers"] is None
    assert out["genres"] is None


def _test_map_missing_title_defaults():
    scene = dict(SCENE_FIXTURE, title=None)
    out = mapping.scene_to_movie_dict(scene)
    assert out["title"] == "Untitled"


def _test_map_iso_datetime():
    scene = dict(SCENE_FIXTURE, date="2024-05-01T00:00:00")
    out = mapping.scene_to_movie_dict(scene)
    assert out["aired_at"] == approx_datetime(2024, 5, 1)
    assert out["year"] == 2024


def _test_map_no_date():
    scene = dict(SCENE_FIXTURE, date=None)
    out = mapping.scene_to_movie_dict(scene)
    assert out["aired_at"] is None
    assert out["year"] is None


# ---------------------------------------------------------------------------
# 2. API client tests
# ---------------------------------------------------------------------------

api_mod = _load_real("tpdb_api_real", SRC / "program" / "apis" / "tpdb_api.py")


def _test_api_model_parsing():
    scene = api_mod.TpdbScene.model_validate(SCENE_FIXTURE)
    assert scene.id == "8f3b2c1d-0000-1111-2222-333344445555"
    assert scene.site.name == "Example Studios"
    assert scene.site.uuid == "site-uuid-123"
    assert [p.name for p in scene.performers] == ["Performer One", "Performer Two"]
    assert [t.name for t in scene.tags] == ["Big Tits", "Romance"]
    assert scene.directors[0].id == 13122  # live API returns int id
    assert scene.directors[0].name == "Director One"


def _test_api_movie_model_parsing():
    movie = api_mod.TpdbMovie.model_validate(MOVIE_FIXTURE)
    assert movie.id == "movie-uuid-999"
    assert movie.title == "Example Full Movie"
    assert movie.directors[0].id == 13122
    assert movie.directors[0].name == "Director One"
    assert [t.name for t in movie.tags] == ["Feature"]


def _test_api_extra_fields_preserved():
    # `extra="allow"` must keep undeclared fields through model_dump()
    payload = dict(SCENE_FIXTURE, some_future_field="kept", nested={"a": 1})
    scene = api_mod.TpdbScene.model_validate(payload)
    dumped = scene.model_dump()
    assert dumped["some_future_field"] == "kept"
    assert dumped["nested"] == {"a": 1}


def _test_api_parse_helpers():
    assert len(api_mod.TpdbApi._parse_many({"data": [SCENE_FIXTURE, SCENE_FIXTURE]}, api_mod.TpdbScene)) == 2
    assert isinstance(api_mod.TpdbApi._parse_one({"data": SCENE_FIXTURE}, api_mod.TpdbScene), api_mod.TpdbScene)
    assert api_mod.TpdbApi._parse_many({"data": {}}, api_mod.TpdbScene) == []
    assert api_mod.TpdbApi._parse_one({"data": []}, api_mod.TpdbScene) is None


def _test_api_headers_and_base_url():
    api = api_mod.TpdbApi(api_base_url="https://api.theporndb.net/", api_token="")
    assert api.session.base_url == "https://api.theporndb.net"
    assert "Authorization" not in api.session.headers
    api2 = api_mod.TpdbApi(api_token="sekret")
    assert api2.session.headers["Authorization"] == "Bearer sekret"


def _test_api_search_url_building():
    captured = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"data": [SCENE_FIXTURE]}

    api = api_mod.TpdbApi(api_token="x")

    def fake_get(url):
        captured["url"] = url
        return FakeResp()

    api.session.get = fake_get
    result = api.search_scenes(title="Example", oshash=None, year=None)
    assert len(result) == 1
    assert captured["url"] == "scenes?parse=Example"


def _test_api_error_on_401():
    class FakeResp:
        status_code = 401
        text = '{"message":"Unauthenticated."}'

    api = api_mod.TpdbApi(api_token="")
    api.session.get = lambda url: FakeResp()
    try:
        api.search_scenes(title="x")
    except api_mod.TpdbApiError as exc:
        assert "401" in str(exc)
    else:
        raise AssertionError("expected TpdbApiError")


# ---------------------------------------------------------------------------
# 3. Indexer control-flow tests (with stubs)
# ---------------------------------------------------------------------------

class _StubLogger:
    def __getattr__(self, _):
        return lambda *a, **k: None


_T = TypeVar("_T")
_MediaItemGenerator = Generator[_T, None, None]


class _Di:
    def __init__(self):
        self._services = {}

    def __setitem__(self, key, value):
        self._services[key] = value

    def __getitem__(self, key):
        return self._services[key]


class _Dumpable:
    def __init__(self, data):
        self._data = data

    def model_dump(self):
        return self._data


class _FakeApi:
    def __init__(self, scene=None, movie=None):
        self._scene = scene
        self._movie = movie
        self.get_scene_calls = []
        self.get_movie_calls = []

    def get_scene(self, uuid):
        self.get_scene_calls.append(uuid)
        return _Dumpable(self._scene) if self._scene is not None else None

    def get_movie(self, movie_id):
        self.get_movie_calls.append(movie_id)
        return _Dumpable(self._movie) if self._movie is not None else None


def _build_indexer_env():
    # Stub framework modules so we can import the real tpdb_indexer without the
    # full application dependency tree (SQLAlchemy, kink, RTN, DB, etc.).
    stub_logger = _StubLogger()
    di = _Di()

    _stub("kink", di=di)
    _stub("loguru", logger=stub_logger)
    _stub("program", **{})
    _stub("program.apis", **{})
    _stub("program.apis.tpdb_api", TpdbApi=object)
    _stub("program.core", **{})
    _stub("program.core.runner", MediaItemGenerator=_MediaItemGenerator, RunnerResult=RunnerResult)
    _stub("program.media", **{})
    _stub("program.media.item", MediaItem=MediaItem, Movie=Movie)
    _stub("program.services", **{})
    _stub("program.services.indexers", **{})
    _stub("program.services.indexers.base", BaseIndexer=BaseIndexer)
    # Real mapping module
    _load_real(
        "program.services.indexers.tpdb_mapping",
        SRC / "program" / "services" / "indexers" / "tpdb_mapping.py",
    )

    idx_mod = _load_real(
        "program.services.indexers.tpdb_indexer",
        SRC / "program" / "services" / "indexers" / "tpdb_indexer.py",
    )

    return idx_mod, di


def _indexer(di, scene=None, movie=None):
    fake = _FakeApi(scene=scene, movie=movie)
    di[TpdbApi] = fake
    return TPDBIndexer(), fake


class MediaItem:
    def __init__(self, item=None):
        self.tpdb_id = None
        self.type = "mediaitem"
        self.title = ""
        self.requested_by = None
        self.requested_at = None
        self.poster_path = None
        self.year = None
        self.site_id = None
        self.site_name = None
        self.performers = None
        self.genres = None
        self.aired_at = None
        self.rating = None
        self.indexed_at = None
        if item:
            for key, value in item.items():
                setattr(self, key, value)

    @property
    def log_string(self):
        return self.title or "unknown"


class Movie(MediaItem):
    def __init__(self, item=None):
        super().__init__(item)
        self.type = "movie"


class BaseIndexer:
    def __init__(self):
        self.initialized = True

    @staticmethod
    def copy_attributes(source, target):
        for attr in ("requested_by", "requested_at"):
            if hasattr(source, attr):
                setattr(target, attr, getattr(source, attr))

    def copy_items(self, item_a, item_b):
        self.copy_attributes(item_a, item_b)
        return item_b


@dataclass
class RunnerResult:
    media_items: list
    run_at: datetime | None = None


_idx_mod, _di = _build_indexer_env()
TPDBIndexer = _idx_mod.TPDBIndexer
TpdbApi = _idx_mod.TpdbApi if hasattr(_idx_mod, "TpdbApi") else object


def _run(idx, item):
    return list(idx.run(item))


def _test_indexer_missing_tpdb_id():
    idx, _ = _indexer(_di, scene=SCENE_FIXTURE)
    assert _run(idx, MediaItem({})) == []


def _test_indexer_wrong_type():
    idx, _ = _indexer(_di, scene=SCENE_FIXTURE)
    assert _run(idx, MediaItem({"tpdb_id": "x", "type": "show"})) == []


def _test_indexer_fresh_index_scene():
    idx, fake = _indexer(_di, scene=SCENE_FIXTURE)
    results = _run(idx, MediaItem({"tpdb_id": "scene-1", "requested_by": "test"}))
    assert len(results) == 1
    movie = results[0].media_items[0]
    assert isinstance(movie, Movie)
    assert movie.title == "Example Scene Title"
    assert movie.tpdb_id == "8f3b2c1d-0000-1111-2222-333344445555"
    assert movie.site_name == "Example Studios"
    assert movie.performers == ["Performer One", "Performer Two"]
    assert movie.requested_by == "test"  # copy_items propagated metadata
    assert movie.indexed_at is not None
    assert fake.get_scene_calls == ["scene-1"]
    assert fake.get_movie_calls == []


def _test_indexer_fresh_index_movie_fallback():
    idx, fake = _indexer(_di, scene=None, movie=MOVIE_FIXTURE)
    results = _run(idx, MediaItem({"tpdb_id": "movie-1"}))
    assert len(results) == 1
    movie = results[0].media_items[0]
    assert movie.title == "Example Full Movie"
    assert movie.tpdb_id == "movie-uuid-999"
    assert fake.get_scene_calls == ["movie-1"]
    assert fake.get_movie_calls == ["movie-1"]


def _test_indexer_reindex_updates_in_place():
    idx, _ = _indexer(_di, scene=SCENE_FIXTURE)
    existing = Movie({"tpdb_id": "scene-1", "title": "Old Title", "year": 1999})
    results = _run(idx, existing)
    assert len(results) == 1
    updated = results[0].media_items[0]
    assert updated is existing  # same instance, updated in place
    assert existing.title == "Example Scene Title"
    assert existing.year == 2024


def _test_indexer_not_found():
    idx, _ = _indexer(_di, scene=None, movie=None)
    assert _run(idx, MediaItem({"tpdb_id": "missing"})) == []


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    tests = [
        ("mapping: full scene", _test_map_full_scene),
        ("mapping: movie", _test_map_movie),
        ("mapping: site_id int fallback", _test_map_site_id_fallback_to_int),
        ("mapping: no site", _test_map_no_site),
        ("mapping: empty lists -> None", _test_map_empty_lists_become_none),
        ("mapping: missing title -> Untitled", _test_map_missing_title_defaults),
        ("mapping: ISO datetime date", _test_map_iso_datetime),
        ("mapping: no date", _test_map_no_date),
        ("api: model parsing", _test_api_model_parsing),
        ("api: movie model parsing (int director id)", _test_api_movie_model_parsing),
        ("api: extra fields preserved", _test_api_extra_fields_preserved),
        ("api: parse helpers", _test_api_parse_helpers),
        ("api: headers + base_url", _test_api_headers_and_base_url),
        ("api: search URL building", _test_api_search_url_building),
        ("api: error on 401", _test_api_error_on_401),
        ("indexer: missing tpdb_id", _test_indexer_missing_tpdb_id),
        ("indexer: wrong type skipped", _test_indexer_wrong_type),
        ("indexer: fresh index scene", _test_indexer_fresh_index_scene),
        ("indexer: fresh index movie fallback", _test_indexer_fresh_index_movie_fallback),
        ("indexer: reindex in place", _test_indexer_reindex_updates_in_place),
        ("indexer: not found", _test_indexer_not_found),
    ]

    for name, fn in tests:
        check(name, fn)

    print(f"\nPASSED: {len(PASSED)}")
    print(f"FAILED: {len(FAILED)}")
    for name, reason in FAILED:
        print(f"  ✗ {name}: {reason}")
    for name in PASSED:
        print(f"  ✓ {name}")

    # Live smoke test (optional, requires TPDB_API_TOKEN)
    token = os.environ.get("TPDB_API_TOKEN")
    if token:
        live_api = api_mod.TpdbApi(api_token=token)
        try:
            scenes = live_api.search_scenes(title="brazzers")
            print(f"\nLIVE TPDB search returned {len(scenes)} results")
            if scenes:
                first = scenes[0]
                print(f"  first: id={first.id!r} title={first.title!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"\nLIVE TPDB search FAILED: {type(exc).__name__}: {exc}")
    else:
        print("\nLIVE TPDB test SKIPPED (set TPDB_API_TOKEN to enable; endpoint "
              "returns 401 without a token)")

    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
