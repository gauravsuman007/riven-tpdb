"""Which CDN answers mean "this link is spent, mint another".

A debrid link expires, and every provider says so differently. TorBox answers
a spent `/dld/` link with **400 Bad Request**; others use 401, 404 or 410; a
503 is what a link pointing at a node that has gone away looks like. The VFS
re-minted only on 404/410/503, so on TorBox every file whose stored link had
expired failed its read outright ("Unexpected HTTP 400") -- a media server
scanning the mount saw I/O errors on files that play fine through the API,
which already re-mints on any 4xx but 429.

403 is NOT here. The VFS treats it as rate limiting or auth and backs off
instead, because minting more links for a file the CDN is refusing only
spends API calls; see media_stream.py.

Import-free, so the rule is testable without the VFS (src/tests/test_link_status.py).
"""

SPENT_LINK_STATUSES = frozenset({400, 401, 404, 410, 503})


def link_is_spent(status_code: int) -> bool:
    """True when a fresh link for the same file is worth one retry."""

    return status_code in SPENT_LINK_STATUSES
