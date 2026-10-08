"""One HTTP client per event loop, never one shared across two.

THE BUG THIS EXISTS FOR
-----------------------
Every direct stream (`/api/v1/stream/file/{id}`) failed with a 502 and the log
line "Failed to connect to upstream ...: must be called from async context",
for every title, until the process was restarted -- and sometimes straight
after a restart too.

This process runs two event loops. FastAPI serves requests on asyncio; the VFS
(pyfuse3) runs on trio in its own thread. Both took the SAME `httpx.AsyncClient`
out of `di`. httpcore creates its connection pool's lock lazily, on first use,
for whichever async library it detects at that moment, and keeps it. So the
first loop to make a request decides the pool for the life of the process:
the VFS reading first (a media server scanning the mount is enough) left a trio
lock that every later asyncio request tried to acquire, and trio refuses with
exactly that message. The other order breaks the VFS instead.

`AsyncClient.send` forces sniffio's context variable to "asyncio", which reads
as if it prevents this. It cannot: sniffio consults trio's per-THREAD marker
before that context variable, so inside the VFS thread it still says "trio",
which is the right answer there -- the pool simply must not be shared.

So the VFS takes its clients from here, one per thread and per kind. The API
keeps the lifespan's clients in `di`. Reproduced and covered by
`src/tests/test_loop_client.py`.

Deliberately free of program imports, so the rule can be tested without a
settings file or a database.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

_local = threading.local()


def client_for_this_loop(kind: str, factory: Callable[[], T]) -> T:
    """The client of `kind` owned by the calling thread's event loop.

    Keyed by thread because each loop here runs in a thread of its own; a
    second call from the same loop gets the same client, so connections are
    still pooled within it.
    """

    clients: dict[str, T] | None = getattr(_local, "clients", None)

    if clients is None:
        clients = {}
        _local.clients = clients

    client = clients.get(kind)

    if client is None:
        client = factory()
        clients[kind] = client

    return client
