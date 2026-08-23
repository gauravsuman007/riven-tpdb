"""Tests for the TorBox downloader port.

The interesting part of this port is the synthetic link. TorBox mints playable
URLs from a ``(torrent_id, file_id)`` pair and those URLs expire, so
``download_url`` stores ``torbox://{torrent_id}/{file_id}`` and
``unrestrict_link`` resolves it on demand. These tests cover that round trip and
the response-shape handling, using a stub session so no API key or network is
needed.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

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


from program.services.downloaders.torbox import (  # noqa: E402
    LINK_SCHEME,
    TorBoxDownloader,
    TorBoxError,
)


class StubResponse:
    def __init__(self, payload, ok=True, status_code=200, reason="OK"):
        self._payload = payload
        self.ok = ok
        self.status_code = status_code
        self.reason = reason

    @property
    def data(self):
        return self._payload


class StubSession:
    """Records calls and replays queued responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def _next(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return self._responses.pop(0)

    def get(self, path, **kwargs):
        return self._next("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self._next("POST", path, **kwargs)


def make_downloader(responses):
    """Build a downloader without running __init__ (which would validate)."""

    dl = TorBoxDownloader.__new__(TorBoxDownloader)
    dl.key = "torbox"
    dl.api = SimpleNamespace(api_key="test-key", session=StubSession(responses))
    return dl


# --------------------------------------------------------------- link format


def _test_link_roundtrip():
    link = TorBoxDownloader.build_link("12345", 7)
    assert link == f"{LINK_SCHEME}12345/7", link
    assert TorBoxDownloader.parse_link(link) == ("12345", 7)


def _test_link_roundtrip_int_id():
    link = TorBoxDownloader.build_link(99, 0)
    assert TorBoxDownloader.parse_link(link) == ("99", 0)


def _test_parse_rejects_foreign_links():
    # A Real-Debrid URL must not be mistaken for a TorBox reference; the VFS
    # calls unrestrict_link with whatever the entry holds.
    for bad in [
        "https://real-debrid.com/d/ABCDEF",
        "",
        "torbox://",
        "torbox://onlyid",
        "torbox://12345/notanint",
    ]:
        assert TorBoxDownloader.parse_link(bad) is None, bad


# -------------------------------------------------------------- unrestricting


def _test_unrestrict_resolves_fresh_url():
    dl = make_downloader([StubResponse({"data": "https://cdn.torbox.app/x/file.mkv"})])
    result = dl.unrestrict_link(TorBoxDownloader.build_link("555", 2))

    assert result is not None
    assert result.download == "https://cdn.torbox.app/x/file.mkv"
    assert result.filename == "file.mkv"

    method, path, kwargs = dl.api.session.calls[0]
    assert method == "GET" and path == "torrents/requestdl", (method, path)
    params = kwargs["params"]
    assert params["torrent_id"] == "555", params
    assert params["file_id"] == 2, params
    assert params["token"] == "test-key", params


def _test_unrestrict_strips_query_from_filename():
    dl = make_downloader(
        [StubResponse({"data": "https://cdn.torbox.app/x/file.mkv?token=abc"})]
    )
    result = dl.unrestrict_link(TorBoxDownloader.build_link("1", 0))
    assert result.filename == "file.mkv", result.filename


def _test_unrestrict_returns_none_for_foreign_link():
    dl = make_downloader([])
    assert dl.unrestrict_link("https://real-debrid.com/d/XYZ") is None
    # Must not have called the API at all.
    assert dl.api.session.calls == []


def _test_unrestrict_handles_error_response():
    dl = make_downloader([StubResponse({}, ok=False, status_code=404, reason="Not Found")])
    assert dl.unrestrict_link(TorBoxDownloader.build_link("1", 0)) is None


# ------------------------------------------------------------------ torrents


def _test_add_torrent_returns_id():
    dl = make_downloader([StubResponse({"data": {"torrent_id": 4242}})])
    assert dl.add_torrent("ABCDEF") == "4242"

    method, path, kwargs = dl.api.session.calls[0]
    assert method == "POST" and path == "torrents/createtorrent"
    assert kwargs["data"]["magnet"] == "magnet:?xt=urn:btih:abcdef"


def _test_add_torrent_raises_without_id():
    dl = make_downloader([StubResponse({"data": {}})])
    try:
        dl.add_torrent("ABCDEF")
    except TorBoxError:
        return
    raise AssertionError("expected TorBoxError")


def _test_get_torrent_info_builds_synthetic_links():
    dl = make_downloader(
        [
            StubResponse(
                {
                    "data": {
                        "id": 77,
                        "name": "Some.Title.1080p",
                        "hash": "abc123",
                        "size": 1234,
                        "download_state": "completed",
                        "files": [
                            {"id": 0, "name": "a/b.mkv", "size": 900},
                            {"id": 1, "name": "a/c.mkv", "size": 800},
                        ],
                    }
                }
            )
        ]
    )
    info = dl.get_torrent_info(77)

    assert info.id == 77
    assert info.status == "completed"
    assert set(info.files) == {0, 1}
    assert info.files[0].download_url == f"{LINK_SCHEME}77/0", info.files[0].download_url
    assert info.files[1].download_url == f"{LINK_SCHEME}77/1"
    # Round trip: what get_torrent_info stored must be resolvable later.
    assert TorBoxDownloader.parse_link(info.files[1].download_url) == ("77", 1)


def _test_get_torrent_info_unwraps_list():
    dl = make_downloader(
        [StubResponse({"data": [{"id": 5, "name": "X", "files": []}]})]
    )
    assert dl.get_torrent_info(5).id == 5


def _test_select_files_is_noop():
    dl = make_downloader([])
    assert dl.select_files("1", [0, 1]) is None
    assert dl.api.session.calls == []


# ---------------------------------------------------------------------- user


def _test_user_info_premium():
    dl = make_downloader(
        [
            StubResponse(
                {
                    "data": {
                        "id": 12,
                        "email": "x@example.com",
                        "plan": 2,
                        "premium_expires_at": "2030-01-01T00:00:00Z",
                    }
                }
            )
        ]
    )
    info = dl.get_user_info()
    assert info.service == "torbox"
    assert info.premium_status == "premium"
    assert info.premium_expires_at.year == 2030
    assert info.premium_days_left > 0


def _test_user_info_free_plan_is_not_premium():
    dl = make_downloader([StubResponse({"data": {"id": 12, "plan": 0}})])
    info = dl.get_user_info()
    assert info.premium_status == "free", info.premium_status


def _test_user_info_survives_bad_expiry():
    dl = make_downloader(
        [StubResponse({"data": {"id": 1, "plan": 1, "premium_expires_at": "nonsense"}})]
    )
    info = dl.get_user_info()
    assert info is not None
    assert info.premium_expires_at is None


TESTS = [
    ("torbox: link round trip", _test_link_roundtrip),
    ("torbox: link round trip with int id", _test_link_roundtrip_int_id),
    ("torbox: parse rejects foreign links", _test_parse_rejects_foreign_links),
    ("torbox: unrestrict resolves fresh url", _test_unrestrict_resolves_fresh_url),
    ("torbox: unrestrict strips query from filename", _test_unrestrict_strips_query_from_filename),
    ("torbox: unrestrict ignores foreign link", _test_unrestrict_returns_none_for_foreign_link),
    ("torbox: unrestrict handles error", _test_unrestrict_handles_error_response),
    ("torbox: add_torrent returns id", _test_add_torrent_returns_id),
    ("torbox: add_torrent raises without id", _test_add_torrent_raises_without_id),
    ("torbox: torrent info builds synthetic links", _test_get_torrent_info_builds_synthetic_links),
    ("torbox: torrent info unwraps list", _test_get_torrent_info_unwraps_list),
    ("torbox: select_files is a no-op", _test_select_files_is_noop),
    ("torbox: user info premium", _test_user_info_premium),
    ("torbox: free plan is not premium", _test_user_info_free_plan_is_not_premium),
    ("torbox: survives unparseable expiry", _test_user_info_survives_bad_expiry),
]


def main():
    for name, fn in TESTS:
        check(name, fn)

    print(f"\nPASSED: {len(PASSED)}")
    print(f"FAILED: {len(FAILED)}")
    for name in PASSED:
        print(f"  ✓ {name}")
    for name, err in FAILED:
        print(f"  ✗ {name}: {err}")

    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
