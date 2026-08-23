"""Tests for the TPDB discovery client additions.

Focus is the request-building layer, because that is where the live API fails
silently rather than loudly: TPDB accepts unknown query parameters and returns
an unfiltered page instead of an error. A filter that is quietly dropped looks
exactly like a filter that matched everything, so these tests assert on the
exact URL rather than on response shape.

Run directly: ``python src/tests/test_tpdb_discovery.py``
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))


def _load_real(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


api_mod = _load_real("tpdb_api_discovery", SRC / "program" / "apis" / "tpdb_api.py")

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []

SITE_FIXTURE = {"id": 92, "uuid": "e3b61b3e-0c20-4bea-9441-b88430ed6317", "name": "Brazzers"}
SCENE_FIXTURE = {"id": "abc", "title": "A Scene"}
TAG_FIXTURE = {"id": 1, "name": "Massage"}


def _client(payloads: list[dict], captured: list[str]):
    """A client whose session records URLs and replays queued payloads."""

    class FakeResp:
        status_code = 200

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    api = api_mod.TpdbApi(api_token="x")
    queue = list(payloads)

    def fake_get(url):
        captured.append(url)
        return FakeResp(queue.pop(0) if queue else {"data": []})

    api.session.get = fake_get
    return api


def _test_list_scenes_uses_site_id_not_site():
    """Regression: ``site=`` is silently ignored upstream; ``site_id=`` filters."""

    captured: list[str] = []
    api = _client([{"data": [SCENE_FIXTURE]}], captured)
    api.list_scenes(site_id=92, per_page=10)

    url = captured[0]
    assert "site_id=92" in url, url
    assert "site=92" not in url.replace("site_id=92", ""), url


def _test_list_scenes_resolves_uuid_to_numeric_id():
    """A UUID costs one /sites lookup, then filters on the numeric id."""

    captured: list[str] = []
    api = _client([{"data": SITE_FIXTURE}, {"data": [SCENE_FIXTURE]}], captured)
    api.list_scenes(site_id="e3b61b3e-0c20-4bea-9441-b88430ed6317")

    assert captured[0] == "sites/e3b61b3e-0c20-4bea-9441-b88430ed6317", captured
    assert "site_id=92" in captured[1], captured


def _test_site_id_resolution_is_cached():
    captured: list[str] = []
    api = _client(
        [{"data": SITE_FIXTURE}, {"data": [SCENE_FIXTURE]}, {"data": [SCENE_FIXTURE]}],
        captured,
    )
    api.list_scenes(site_id="e3b61b3e-0c20-4bea-9441-b88430ed6317")
    api.list_scenes(site_id="e3b61b3e-0c20-4bea-9441-b88430ed6317")

    # One lookup, two scene calls -- not two lookups.
    assert sum(1 for u in captured if u.startswith("sites/")) == 1, captured


def _test_numeric_string_needs_no_lookup():
    captured: list[str] = []
    api = _client([{"data": [SCENE_FIXTURE]}], captured)
    api.list_scenes(site_id="92")

    assert len(captured) == 1, captured
    assert "site_id=92" in captured[0], captured


def _test_unfiltered_listing_sends_no_site_param():
    captured: list[str] = []
    api = _client([{"data": [SCENE_FIXTURE]}], captured)
    api.list_scenes(page=2, per_page=50)

    assert "site_id" not in captured[0], captured
    assert "page=2" in captured[0] and "per_page=50" in captured[0], captured


def _test_text_search_uses_q():
    """``q`` is the only parameter that genuinely narrows a scene listing."""

    captured: list[str] = []
    api = _client([{"data": [SCENE_FIXTURE]}], captured)
    api.search_scenes_text("massage", per_page=5)

    assert "q=massage" in captured[0], captured


def _test_list_movies_and_tags():
    captured: list[str] = []
    api = _client([{"data": [SCENE_FIXTURE]}, {"data": [TAG_FIXTURE]}], captured)
    api.list_movies(page=3)
    tags = api.list_tags(per_page=100)

    assert captured[0].startswith("movies?") and "page=3" in captured[0], captured
    assert captured[1].startswith("tags?") and "per_page=100" in captured[1], captured
    assert tags[0].name == "Massage"


def _test_error_carries_status_code():
    class FakeResp:
        status_code = 404
        text = '{"message":"Not found"}'

    api = api_mod.TpdbApi(api_token="x")
    api.session.get = lambda url: FakeResp()

    try:
        api.get_site("nope")
    except api_mod.TpdbApiError as exc:
        assert exc.status_code == 404, exc.status_code
    else:
        raise AssertionError("expected TpdbApiError")


def _test_resolution_failure_returns_none():
    captured: list[str] = []
    api = _client([{"data": None}], captured)
    assert api.resolve_site_id("00000000-0000-0000-0000-000000000000") is None


def check(name, fn):
    try:
        fn()
    except AssertionError as exc:
        FAILED.append((name, str(exc)))
    except Exception as exc:  # noqa: BLE001
        FAILED.append((name, f"{type(exc).__name__}: {exc}"))
    else:
        PASSED.append(name)


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("_test_") and callable(fn):
            check(name, fn)

    print(f"\nPASSED: {len(PASSED)}")
    print(f"FAILED: {len(FAILED)}")
    for name, reason in FAILED:
        print(f"  ✗ {name}: {reason}")
    for name in PASSED:
        print(f"  ✓ {name}")

    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
