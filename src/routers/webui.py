r"""Reverse proxy the normal Riven web UI onto the Jellyfin port.

The Jellyfin WebView shells (official Jellyfin for Android, LG webOS) ship no
interface -- they load one from whatever server they connected to. Serving the
real frontend here means those apps show the same UI as a browser, rather than
a second, separately-maintained one.

Why proxy instead of redirecting the WebView to the frontend's own port: the
app keeps using the address it was given as its API base, and `localStorage`
is per-origin. Sending the WebView to another origin splits the credentials
the native player reads from the API it calls. Proxying keeps one origin, so
the UI, the Jellyfin API, and the media stream all agree.

Two things this deliberately does NOT do:

- It does not follow redirects. The frontend answers `307 -> /auth/login` for
  a signed-out client and the browser has to see that itself, or login breaks.
- It does not touch `/api/`. The backend's own API is mounted there and must
  keep winning; this only picks up what nothing else claimed.

`X-Forwarded-For` is set so the frontend can still identify the real client,
but adapter-node only honours it when `ADDRESS_HEADER` is configured. Without
that, the frontend's local-network bypass sees this server's address instead,
does not trust it, and shows the login screen -- it fails closed, which is the
right direction for an auth bypass, but it does mean signing in by hand.
"""

from typing import Any

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse
from loguru import logger

from program.services.jellyfin_server import webapp
from program.settings import settings_manager

router = APIRouter(include_in_schema=False)

#: Hop-by-hop headers, which a proxy must not forward (RFC 7230 6.1).
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

#: Injected into every proxied HTML document. The Jellyfin WebView marks
#: itself connected when it sees a request whose PATH matches
#: `.*/main\.[^/\s]+\.bundle\.js`, so the frontend's own pages need to ask for
#: ours -- otherwise the app sits on a spinner for 10s and gives up. Harmless
#: in a normal browser, where it is just a small script.
_INJECT = f'<script src="{webapp.BUNDLE_PATH}" defer></script>'


def base_url() -> str:
    """Where the web UI lives, or empty when the feature is off."""

    settings = settings_manager.settings.jellyfin_server

    if not settings.enabled:
        return ""

    return (settings.web_ui_url or "").rstrip("/")


def is_configured() -> bool:
    return bool(base_url())


def _forward_headers(request: Request) -> dict[str, str]:
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _HOP_BY_HOP and key.lower() != "host"
    }

    client_host = request.client.host if request.client else None

    if client_host:
        existing = request.headers.get("x-forwarded-for")
        headers["x-forwarded-for"] = (
            f"{existing}, {client_host}" if existing else client_host
        )

    headers["x-forwarded-proto"] = request.url.scheme

    if request.headers.get("host"):
        headers["x-forwarded-host"] = request.headers["host"]

    # SvelteKit's CSRF check compares the browser's Origin header against the
    # frontend's OWN `ORIGIN` env var -- a fixed string, not something
    # X-Forwarded-Host can influence. A Jellyfin app connecting through THIS
    # server sends Origin: <this server>, which never matches, so every form
    # POST (login included) is silently rejected as cross-site. Rewriting it
    # to the value the frontend actually trusts is the only fix that does not
    # touch the frontend container or weaken its CSRF check for direct
    # (non-proxied) visitors.
    trusted_origin = settings_manager.settings.jellyfin_server.web_ui_origin

    if trusted_origin:
        if "origin" in headers:
            headers["origin"] = trusted_origin

        referer = headers.get("referer")

        if referer:
            headers["referer"] = referer.replace(
                f"{request.url.scheme}://{request.url.netloc}", trusted_origin, 1
            )

    return headers


def _response_headers(source: httpx.Response) -> list[tuple[str, str]]:
    """Headers to relay, preserving repeats.

    A LOGIN RESPONSE SETS THREE SEPARATE `Set-Cookie` HEADERS. Returning a
    `dict` here (the original shape) silently drops all but one of them --
    Python dict keys are unique, so three `(set-cookie, ...)` pairs collapse
    to whichever came last, and the client never receives the actual session
    cookie. That is exactly what made a proxied login appear to succeed (200,
    no error) while every subsequent request still looked signed out: only a
    disposable 60s-lived cookie was making it through, never the real one.
    A `list[tuple]` plus `MutableHeaders.append` (below) is what actually
    preserves every value.
    """

    return [
        (key, value)
        for key, value in source.headers.multi_items()
        # Content-Length is dropped because injection changes the body, and
        # Content-Encoding because httpx has already decompressed it.
        if key.lower() not in _HOP_BY_HOP
        and key.lower() not in {"content-length", "content-encoding"}
    ]


def _apply_headers(response: Response, headers: list[tuple[str, str]]) -> Response:
    """Attach possibly-repeated headers to a Response without collapsing them.

    `Response(headers=...)` only accepts a single-value mapping; appending
    through `response.headers` (Starlette's `MutableHeaders`) adds a new
    header line per call instead of overwriting, which is required for
    multiple `Set-Cookie` values to survive.
    """

    for key, value in headers:
        response.headers.append(key, value)

    return response


async def proxy(request: Request, path: str) -> Response:
    """Forward one request to the web UI and return its response."""

    target = base_url()

    if not target:
        return Response(status_code=404, content="Not found")

    url = f"{target}/{path.lstrip('/')}"

    try:
        client: httpx.AsyncClient = httpx.AsyncClient(timeout=30.0, follow_redirects=False)

        body = await request.body()

        upstream = await client.request(
            request.method,
            url,
            params=request.query_params,
            headers=_forward_headers(request),
            content=body or None,
        )
    except Exception as exc:
        logger.warning(f"Web UI proxy could not reach {url}: {exc}")

        return Response(
            status_code=502,
            content=(
                "Riven's web UI is not reachable from this server. Check "
                "Settings -> Jellyfin server -> web_ui_url."
            ),
            media_type="text/plain",
        )

    content_type = upstream.headers.get("content-type", "")
    headers = _response_headers(upstream)

    # Only HTML documents get the injection; assets stream through untouched.
    if "text/html" in content_type.lower():
        text = upstream.text
        await client.aclose()

        if webapp.BUNDLE_PATH not in text:
            if "</head>" in text:
                text = text.replace("</head>", f"{_INJECT}</head>", 1)
            elif "<body" in text:
                index = text.index("<body")
                text = text[:index] + _INJECT + text[index:]
            else:
                text = _INJECT + text

        return _apply_headers(
            Response(
                content=text,
                status_code=upstream.status_code,
                media_type=content_type,
            ),
            headers,
        )

    async def stream() -> Any:
        try:
            yield upstream.content
        finally:
            await client.aclose()

    return _apply_headers(
        StreamingResponse(
            stream(),
            status_code=upstream.status_code,
            media_type=content_type or None,
        ),
        headers,
    )


# Registered last in main.py, so it only ever sees paths that the API and the
# Jellyfin surface did not claim.
@router.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def web_ui(request: Request, full_path: str) -> Response:
    if not is_configured():
        return Response(status_code=404, content="Not found")

    # The backend's own API owns /api/ and must not be shadowed if a path
    # under it happens to be unrouted.
    if full_path.startswith("api/"):
        return Response(status_code=404, content="Not found")

    return await proxy(request, full_path)
