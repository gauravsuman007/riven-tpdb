"""Which CDN statuses make the VFS mint a fresh link.

TorBox answers an expired link with 400, and the VFS used to treat that as a
hard failure -- every file whose stored link had expired was an I/O error on
the mount. Run directly: ``python src/tests/test_link_status.py`` or under
pytest.
"""

import importlib.util
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "link_status", SRC / "program/services/streaming/link_status.py"
)
link_status = importlib.util.module_from_spec(spec)
sys.modules["link_status"] = link_status
spec.loader.exec_module(link_status)


def test_torbox_expired_link_is_spent():
    assert link_status.link_is_spent(400)


def test_the_statuses_that_already_refreshed_still_do():
    for status in (404, 410, 503):
        assert link_status.link_is_spent(status), status


def test_unauthorized_is_spent():
    assert link_status.link_is_spent(401)


def test_rate_limits_and_refusals_are_not():
    # Minting more links for a file the CDN is refusing only spends API calls.
    for status in (403, 429, 416, 500, 200, 206):
        assert not link_status.link_is_spent(status), status


def test_a_placeholder_is_not_a_fresh_link():
    assert not link_status.is_fetchable_link("torbox://12345/67")
    assert link_status.is_fetchable_link("https://nexus-1.tb-cdn.st/dld/x?token=y")


def test_the_vfs_uses_the_rule():
    source = (SRC / "program/services/streaming/media_stream.py").read_text()
    assert "link_is_spent(status_code)" in source
    assert "is_fetchable_link(fresh_url)" in source
    assert "trio.to_thread.run_sync" in source.split("async def _refresh_download_url")[1].split("async def ")[0]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  ok   {name}")
