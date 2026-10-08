"""One HTTP client per event loop: the VFS (trio) and the API (asyncio).

Every direct stream 502'd with "must be called from async context" because
both loops shared one `httpx.AsyncClient`, and httpcore keeps the pool lock of
whichever loop used it first. See services/streaming/loop_client.py.

Real httpx, httpcore and trio against a local server -- the bug lives in how
those three meet, so a mocked transport (which skips httpcore's pool) would
pass with the bug present. Run directly: ``python src/tests/test_loop_client.py``
or under pytest.
"""

import asyncio
import importlib.util
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import sniffio
import trio

SRC = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "loop_client", SRC / "program/services/streaming/loop_client.py"
)
loop_client = importlib.util.module_from_spec(spec)
sys.modules["loop_client"] = loop_client
spec.loader.exec_module(loop_client)


class _Ok(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server's name
        self.send_response(200)
        self.send_header("content-length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


class Forced(httpx.AsyncClient):
    """What program.utils.async_client.AsyncClient does to `send`."""

    async def send(self, request, **kwargs):
        token = sniffio.current_async_library_cvar.set("asyncio")
        try:
            return await super().send(request, **kwargs)
        finally:
            sniffio.current_async_library_cvar.reset(token)


def _serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Ok)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/"


async def _get(client, url):
    response = await client.send(client.build_request("GET", url), stream=True)
    await response.aclose()
    return response.status_code


def _in_trio_thread(fn):
    """Run `fn` (async) on trio in a thread of its own, as the VFS does."""

    out = {}

    def run():
        try:
            out["value"] = trio.run(fn)
        except BaseException as error:  # noqa: BLE001 - carried to the caller
            out["error"] = error

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()

    if "error" in out:
        raise out["error"]

    return out["value"]


def test_a_shared_client_is_broken_for_the_second_loop():
    """The trap itself, so a "simplification" back to one client fails here."""

    server, url = _serve()
    try:
        shared = Forced()
        assert _in_trio_thread(lambda: _get(shared, url)) == 200

        try:
            asyncio.run(_get(shared, url))
        except RuntimeError as error:
            assert "async context" in str(error)
        else:
            raise AssertionError(
                "a client first used on trio served asyncio -- if httpcore "
                "fixed this, loop_client.py can be retired"
            )
    finally:
        server.shutdown()


def test_each_loop_gets_its_own_client_and_both_work():
    server, url = _serve()
    try:
        made = []

        def factory():
            made.append(Forced())
            return made[-1]

        # The VFS first, which is the order that broke the API.
        async def vfs():
            first = loop_client.client_for_this_loop("direct", factory)
            again = loop_client.client_for_this_loop("direct", factory)
            assert first is again, "one loop keeps one pool"
            return await _get(first, url)

        assert _in_trio_thread(vfs) == 200

        async def api():
            return await _get(loop_client.client_for_this_loop("direct", factory), url)

        assert asyncio.run(api()) == 200
        assert len(made) == 2 and made[0] is not made[1]
    finally:
        server.shutdown()


def test_kinds_are_separate():
    a = loop_client.client_for_this_loop("proxy", object)
    b = loop_client.client_for_this_loop("direct-other", object)
    assert a is not b


def test_the_vfs_no_longer_takes_the_api_client():
    """MediaStream must not reach for di[AsyncClient] / di[ProxyClient]."""

    source = (SRC / "program/services/streaming/media_stream.py").read_text()
    assert "di[AsyncClient]" not in source and "di[ProxyClient]" not in source
    assert "client_for_this_loop(" in source


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  ok   {name}")
