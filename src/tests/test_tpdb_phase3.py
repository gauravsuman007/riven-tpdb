"""Self-contained Phase 3 test suite for the TPDB content service.

Verifies the adult-only content provider (site subscriptions) control flow,
pagination, tpdb_id de-duplication, validation, and the `list_scenes` client
endpoint. A live test runs against the real API when `TPDB_API_TOKEN` is set.
"""

import importlib.util
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

SRC = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, SRC)


# ---------------------------------------------------------------------------
# Load + stub infrastructure
# ---------------------------------------------------------------------------

def _load_real(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _stub(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


class _StubLogger:
    def __getattr__(self, _):
        return lambda *a, **k: None


class _Di:
    def __init__(self):
        self._services = {}

    def __setitem__(self, key, value):
        self._services[key] = value

    def __getitem__(self, key):
        return self._services[key]


class Runner:
    def __init__(self):
        self.key = self.get_key()
        self.initialized = False
        self.settings = None

    @classmethod
    def get_key(cls):
        return cls.__name__.lower()

    @property
    def enabled(self):
        s = getattr(self, "settings", None)
        return bool(s and getattr(s, "enabled", True))

    def __class_getitem__(cls, item):
        return cls


class RunnerResult:
    def __init__(self, media_items=None, run_at=None):
        self.media_items = media_items or []
        self.run_at = run_at


class MediaItem:
    def __init__(self, item=None):
        self.tpdb_id = None
        if item:
            for key, value in item.items():
                setattr(self, key, value)

    @property
    def log_string(self):
        return getattr(self, "tpdb_id", "unknown")


# Mutable settings so tests can reconfigure between cases.
_content_tpdb = SimpleNamespace(enabled=True, sites=["site-1", "site-2"], max_pages=3)
_tpdb_cfg = SimpleNamespace(api_token="test-token")
_settings = SimpleNamespace(
    content=SimpleNamespace(tpdb=_content_tpdb),
    tpdb=_tpdb_cfg,
)
settings_manager = SimpleNamespace(settings=_settings)

# Mutable "already in DB" set used by the stubbed item_exists_by_any_id.
_existing_tpdb = set()


def _item_exists_by_any_id(**kwargs):
    return kwargs.get("tpdb_id") in _existing_tpdb


# Load the REAL API module before stubbing `program`, so its
# `from program.utils.request import SmartSession` import resolves against the
# actual filesystem (httpx-backed client, needed by the live test).
api_mod = _load_real("tpdb_api_real", SRC + "/program/apis/tpdb_api.py")


def _build_env():
    di = _Di()

    _stub("kink", di=di)
    _stub("loguru", logger=_StubLogger())
    _stub("program", **{})
    _stub("program.apis", **{})
    _stub("program.apis.tpdb_api", TpdbApi=api_mod.TpdbApi)

    _stub("program.core", **{})
    _stub(
        "program.core.runner",
        MediaItemGenerator=type("MediaItemGenerator", (), {}),
        Runner=Runner,
        RunnerResult=RunnerResult,
    )
    _stub("program.db", **{})
    _stub("program.db.db_functions", item_exists_by_any_id=_item_exists_by_any_id)
    _stub("program.media", **{})
    _stub("program.media.item", MediaItem=MediaItem)
    _stub("program.settings", settings_manager=settings_manager)
    _stub("program.settings.models", TpdbContentModel=type("TpdbContentModel", (), {}))
    _stub("program.services", **{})
    _stub("program.services.content", **{})

    content_mod = _load_real(
        "program.services.content.tpdb_content",
        SRC + "/program/services/content/tpdb_content.py",
    )

    return content_mod, di


content_mod, di = _build_env()

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


def _scene(id_):
    return SimpleNamespace(id=id_)


class _FakeContentApi:
    """Simulates list_scenes with per-site pagination."""

    def __init__(self, pages_by_site):
        self.pages_by_site = pages_by_site
        self.calls = []

    def list_scenes(self, site_id=None, page=None):
        self.calls.append((site_id, page))
        pages = self.pages_by_site.get(site_id, [])
        idx = page - 1 if page else 0
        if 0 <= idx < len(pages):
            return pages[idx]
        return []


def _reset():
    _content_tpdb.enabled = True
    _content_tpdb.sites = ["site-1", "site-2"]
    _content_tpdb.max_pages = 3
    _tpdb_cfg.api_token = "test-token"
    _existing_tpdb.clear()


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

def _test_run_emits_new_tpdb_stubs():
    _reset()
    fake = _FakeContentApi(
        {
            "site-1": [[_scene("s1a"), _scene("s1b")]],
            "site-2": [[_scene("s2a")]],
        }
    )
    di[api_mod.TpdbApi] = fake
    svc = content_mod.TPDBContent()
    assert svc.initialized is True
    result = next(svc.run(None))
    ids = [m.tpdb_id for m in result.media_items]
    assert ids == ["s1a", "s1b", "s2a"]
    assert all(m.requested_by == "tpdb" for m in result.media_items)


def _test_run_skips_existing():
    _reset()
    _existing_tpdb.add("s1b")
    fake = _FakeContentApi({"site-1": [[_scene("s1a"), _scene("s1b"), _scene("s1c")]]})
    di[api_mod.TpdbApi] = fake
    svc = content_mod.TPDBContent()
    result = next(svc.run(None))
    ids = [m.tpdb_id for m in result.media_items]
    assert ids == ["s1a", "s1c"]


def _test_run_dedup_across_pages_and_sites():
    _reset()
    page1 = [_scene("dup")] + [_scene(f"p1-{i}") for i in range(19)]  # full page (20)
    fake = _FakeContentApi(
        {
            "site-1": [page1, [_scene("dup"), _scene("b")]],
            "site-2": [[_scene("dup"), _scene("c")]],
        }
    )
    di[api_mod.TpdbApi] = fake
    svc = content_mod.TPDBContent()
    result = next(svc.run(None))
    ids = [m.tpdb_id for m in result.media_items]
    assert ids.count("dup") == 1
    assert "b" in ids and "c" in ids
    assert len(ids) == len(set(ids)) == 22


def _test_pagination_stops_on_short_page():
    _reset()
    # First page full (20), second page short (5): must not request page 3.
    full = [_scene(f"p1-{i}") for i in range(20)]
    short = [_scene(f"p2-{i}") for i in range(5)]
    fake = _FakeContentApi({"site-1": [full, short]})
    di[api_mod.TpdbApi] = fake
    _content_tpdb.sites = ["site-1"]
    svc = content_mod.TPDBContent()
    result = next(svc.run(None))
    assert len(result.media_items) == 25
    pages = [c[1] for c in fake.calls]
    assert pages == [1, 2]


def _test_pagination_capped_by_max_pages():
    _reset()
    _content_tpdb.max_pages = 2
    pages = [[_scene(f"p{pg}-{i}") for i in range(20)] for pg in range(1, 4)]
    fake = _FakeContentApi({"site-1": pages})
    di[api_mod.TpdbApi] = fake
    _content_tpdb.sites = ["site-1"]
    svc = content_mod.TPDBContent()
    result = next(svc.run(None))
    assert len(result.media_items) == 40
    assert [c[1] for c in fake.calls] == [1, 2]


def _test_validate_requires_token_sites_and_enabled():
    _reset()
    di[api_mod.TpdbApi] = _FakeContentApi({})

    _content_tpdb.enabled = False
    assert content_mod.TPDBContent().validate() is False

    _content_tpdb.enabled = True
    _tpdb_cfg.api_token = ""
    assert content_mod.TPDBContent().validate() is False

    _tpdb_cfg.api_token = "test-token"
    _content_tpdb.sites = []
    assert content_mod.TPDBContent().validate() is False

    _content_tpdb.sites = ["site-1"]
    assert content_mod.TPDBContent().validate() is True


def _test_disabled_service_not_initialized():
    _reset()
    _content_tpdb.enabled = False
    svc = content_mod.TPDBContent()
    assert svc.initialized is False


def _test_get_key():
    assert content_mod.TPDBContent.get_key() == "tpdb"


def _test_list_scenes_url_building():
    captured = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"data": []}

    def fake_get(url):
        captured["url"] = url
        return FakeResp()

    client = api_mod.TpdbApi(api_token="x")
    client.session.get = fake_get
    # A numeric id needs no /sites lookup. The filter parameter is `site_id`;
    # a plain `site` is accepted by the API but silently ignored, which returns
    # the unfiltered global feed instead of erroring.
    client.list_scenes(site_id=1161, page=2)
    assert captured["url"] == "scenes?site_id=1161&page=2", captured["url"]


# ---------------------------------------------------------------------------
# Live test (real API)
# ---------------------------------------------------------------------------

def _test_live_content_run():
    token = os.environ.get("TPDB_API_TOKEN")
    if not token:
        raise AssertionError("LIVE SKIPPED (no TPDB_API_TOKEN)")

    _reset()
    # A real, known site uuid (Brazzers) as the subscription target.
    _content_tpdb.sites = ["e3b61b3e-0c20-4bea-9441-b88430ed6317"]
    _content_tpdb.max_pages = 1
    _existing_tpdb.clear()

    real_api = api_mod.TpdbApi(api_token=token)
    di[api_mod.TpdbApi] = real_api

    svc = content_mod.TPDBContent()
    assert svc.initialized is True
    result = next(svc.run(None))

    items = result.media_items
    assert len(items) > 0
    for m in items:
        assert m.tpdb_id and isinstance(m.tpdb_id, str)
        assert m.requested_by == "tpdb"
    print(f"      -> {len(items)} live scene stubs produced from site subscription")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    ("content: run emits new tpdb stubs", _test_run_emits_new_tpdb_stubs),
    ("content: skips existing (item_exists)", _test_run_skips_existing),
    ("content: dedup across pages/sites", _test_run_dedup_across_pages_and_sites),
    ("content: pagination stops on short page", _test_pagination_stops_on_short_page),
    ("content: pagination capped by max_pages", _test_pagination_capped_by_max_pages),
    ("content: validate token/sites/enabled", _test_validate_requires_token_sites_and_enabled),
    ("content: disabled -> not initialized", _test_disabled_service_not_initialized),
    ("content: get_key == tpdb", _test_get_key),
    ("api: list_scenes URL building", _test_list_scenes_url_building),
]

LIVE_TESTS = [
    ("content: LIVE run against real site", _test_live_content_run),
]


def main():
    for name, fn in TESTS:
        check(name, fn)

    if os.environ.get("TPDB_API_TOKEN"):
        for name, fn in LIVE_TESTS:
            check(name, fn)
    else:
        print("LIVE TPDB test SKIPPED (set TPDB_API_TOKEN to enable)")

    print(f"\nPASSED: {len(PASSED)}")
    print(f"FAILED: {len(FAILED)}")
    for name in PASSED:
        print(f"  \u2713 {name}")
    for name, err in FAILED:
        print(f"  \u2717 {name}: {err}")


if __name__ == "__main__":
    main()