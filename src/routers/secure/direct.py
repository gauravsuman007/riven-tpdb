"""Search streaming sites directly and play what they return.

This is a parallel path to the torrent pipeline, not part of it. Nothing here
touches the library, the debrid provider or the VFS: the user searches, picks a
video, and it plays. It exists because a scene that no indexer carries is often
sitting on a tube site in perfectly watchable quality.

Playback goes through this service rather than straight from the browser for
three reasons, each of which breaks a plain <video src>:

  * the CDNs require a Referer and reject requests without one
  * the URLs are short-lived, and one site binds them to the requesting IP --
    which is this server, not the user's browser
  * none of them send CORS headers

So the browser asks for /direct/stream, and this resolves and proxies. Range
requests are passed through in both directions, so seeking still works.
"""

from typing import Annotated

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel

from program.db.db import db_session
from program.media.item import MediaItem
from program.services.directscrapers import MatchTarget, describe_scrapers
from program.services.directscrapers import reset as reset_direct_service
from program.services.directscrapers import service as direct_service
from program.services.vpn import SCRAPING, STREAMING, VpnUnavailable, vpn
from program.settings import settings_manager


router = APIRouter(
    responses={404: {"description": "Not found"}},
    prefix="/direct",
    tags=["direct"],
)

# `direct_service()` is the process-wide singleton (program/services/directscrapers,
# module-level `service()`/`reset()`) -- looked up per request rather than
# cached at import time here, so a plugin toggle or a rescan takes effect
# on the next search without needing this module reloaded.


class DirectVideoModel(BaseModel):
    site: str
    site_name: str
    video_id: str
    title: str
    page_url: str
    thumbnail: str | None = None
    duration: int | None = None
    resolution: str | None = None
    size: int | None = None
    views: int | None = None
    hd: bool = False
    relevance: float | None = None


class DirectSearchResponse(BaseModel):
    query: str
    results: list[DirectVideoModel]
    errors: dict[str, str]
    """Site key to failure reason. Present so a site being down is visible in
    the UI as "iPornTV failed" rather than as silently fewer results."""


class DirectSourceModel(BaseModel):
    label: str
    mime_type: str
    resolution: str | None = None
    size: int | None = None
    index: int
    """Position in the list, and what /stream takes -- the URL itself is not
    handed to the browser because it expires and would 403 on use."""


class DirectSourcesResponse(BaseModel):
    site: str
    video_id: str
    sources: list[DirectSourceModel]


@router.get("/search", operation_id="direct_search")
def direct_search(
    query: Annotated[str | None, Query(description="Free-text search")] = None,
    item_id: Annotated[
        int | None,
        Query(description="Use this library item's title as the query"),
    ] = None,
    limit: Annotated[
        int | None,
        Query(
            ge=1,
            le=20,
            description=(
                "Maximum results kept per site, after ranking. Defaults to "
                "the Plugins tab's 'Results per site' setting when omitted; "
                "pass this to override it for a single call."
            ),
        ),
    ] = None,
    sites: Annotated[
        str | None, Query(description="Comma-separated site keys")
    ] = None,
) -> DirectSearchResponse:
    search_term = (query or "").strip()
    target: MatchTarget | None = None

    if item_id is not None:
        # db_session is a context manager, not a FastAPI dependency: injecting
        # it hands the route the manager object rather than a Session.
        with db_session() as session:
            item = session.get(MediaItem, item_id)
            if item is None:
                raise HTTPException(status_code=404, detail="Item not found")
            # Performers and studio come along even when the caller supplied
            # its own search text. They are not the query -- they are how a
            # result is recognised once the site has answered, and the site
            # that carries the right series under the wrong episode name is
            # only identifiable by the performer in its title.
            target = MatchTarget.build(
                title=search_term or item.title or "",
                performers=item.performers,
                studio=item.network,
            )

    if target is None:
        if not search_term:
            raise HTTPException(
                status_code=400, detail="Provide either a query or an item_id"
            )
        target = MatchTarget.build(search_term)

    if not target.title:
        raise HTTPException(status_code=400, detail="Nothing to search for")

    # Checked once, up front. The routing itself is enforced inside the
    # scrapers' session, but letting it fail there would surface as eight
    # separate "could not reach this site" errors -- which is true, and
    # useless, because the reason is the same for all of them and is not the
    # sites' fault.
    try:
        vpn().proxy_for(SCRAPING)
    except VpnUnavailable as exc:
        logger.warning(f"Direct search blocked, VPN unavailable: {exc}")
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    selected = [s.strip() for s in sites.split(",")] if sites else None
    per_site = limit if limit is not None else settings_manager.settings.direct_scraping.results_per_site
    results, errors = direct_service().search(target, limit_per_site=per_site, sites=selected)

    return DirectSearchResponse(
        query=target.title,
        results=[
            DirectVideoModel(
                site=video.site,
                site_name=direct_service().services[video.site].name,
                video_id=video.video_id,
                title=video.title,
                page_url=video.page_url,
                thumbnail=video.thumbnail,
                duration=video.duration,
                resolution=video.resolution,
                size=video.size,
                views=video.views,
                hd=video.hd,
                relevance=video.relevance,
            )
            for video in results
        ],
        errors=errors,
    )


@router.get("/sources", operation_id="direct_sources")
def direct_sources(
    site: Annotated[str, Query()],
    video_id: Annotated[str, Query()],
) -> DirectSourcesResponse:
    """List the renditions a video has, without handing out the URLs."""

    try:
        sources = direct_service().resolve(site, video_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning(f"Direct resolve failed for {site}:{video_id}: {exc}")
        raise HTTPException(
            status_code=502, detail="Could not resolve this video"
        ) from exc

    if not sources:
        raise HTTPException(status_code=404, detail="No playable source found")

    return DirectSourcesResponse(
        site=site,
        video_id=video_id,
        sources=[
            DirectSourceModel(
                label=source.label,
                mime_type=source.mime_type,
                resolution=source.resolution,
                size=source.size,
                index=index,
            )
            for index, source in enumerate(sources)
        ],
    )


@router.get("/stream", operation_id="direct_stream")
async def direct_stream(
    request: Request,
    site: Annotated[str, Query()],
    video_id: Annotated[str, Query()],
    index: Annotated[int, Query(ge=0)] = 0,
) -> StreamingResponse:
    """Resolve and proxy one rendition, passing Range through both ways."""

    try:
        sources = direct_service().resolve(site, video_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning(f"Direct resolve failed for {site}:{video_id}: {exc}")
        raise HTTPException(
            status_code=502, detail="Could not resolve this video"
        ) from exc

    if index >= len(sources):
        raise HTTPException(status_code=404, detail="No such source")
    source = sources[index]

    headers = dict(source.headers)
    if "range" in request.headers:
        headers["Range"] = request.headers["range"]

    # Routed separately from the search above: streaming is the bandwidth-heavy
    # half, and wanting searches tunnelled but playback direct (or the reverse)
    # is a reasonable position rather than an edge case.
    try:
        proxy = vpn().proxy_for(STREAMING)
    except VpnUnavailable as exc:
        # Deliberately not falling back to a direct connection. Someone who
        # routes playback is controlling where it appears to come from, and
        # quietly using the host's own address instead would defeat the only
        # reason the setting exists -- invisibly, mid-play.
        logger.warning(f"Direct stream blocked, VPN unavailable: {exc}")
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    client = httpx.AsyncClient(
        follow_redirects=True, timeout=30.0, proxy=proxy
    )
    try:
        upstream = await client.send(
            client.build_request("GET", source.url, headers=headers),
            stream=True,
        )
    except Exception as exc:
        await client.aclose()
        logger.error(f"Direct stream upstream failed for {site}:{video_id}: {exc}")
        raise HTTPException(status_code=502, detail="Upstream connection failed")

    if upstream.status_code >= 400:
        status_code = upstream.status_code
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(
            status_code=502, detail=f"Upstream returned {status_code}"
        )

    response_headers = {
        key: upstream.headers[key]
        for key in ("content-type", "content-length", "content-range")
        if key in upstream.headers
    }
    # Advertised unconditionally: the upstreams all honour Range, and without
    # this header the browser will not offer a seek bar for a fresh stream.
    response_headers["accept-ranges"] = "bytes"

    async def body():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        except Exception as exc:
            logger.debug(f"Direct stream interrupted for {site}:{video_id}: {exc}")
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=response_headers.get("content-type", source.mime_type),
    )


class ScraperInfoModel(BaseModel):
    key: str
    name: str
    base_url: str
    kind: str
    enabled: bool
    source_file: str | None = None
    error: str | None = None


class PluginsResponse(BaseModel):
    plugin_dir: str
    scrapers: list[ScraperInfoModel]


def _plugins_response() -> PluginsResponse:
    return PluginsResponse(
        plugin_dir=settings_manager.settings.direct_scraping.plugin_dir,
        scrapers=[
            ScraperInfoModel(
                key=info.key,
                name=info.name,
                base_url=info.base_url,
                kind=info.kind,
                enabled=info.enabled,
                source_file=info.source_file,
                error=info.error,
            )
            for info in describe_scrapers()
        ],
    )


@router.get("/plugins", operation_id="direct_plugins")
def direct_plugins() -> PluginsResponse:
    """Every known scraper -- built-in and plugin, enabled or not.

    Re-scans the plugin folder on every call. These are a handful of files
    parsed from local disk, not a network request, so there is nothing to
    cache and no reason to make "did my new file show up" depend on a
    separate refresh action.
    """

    return _plugins_response()


@router.post("/plugins/rescan", operation_id="direct_plugins_rescan")
def direct_plugins_rescan() -> PluginsResponse:
    """Drop the cached scraper registry and rebuild it.

    `/plugins` above already re-scans the folder for its own listing, so this
    exists for the other half: making a file drop or an edit to an existing
    plugin take effect in the scrapers actually used by /direct/search,
    without waiting for some other settings change to invalidate them first.
    """

    reset_direct_service()
    return _plugins_response()


class ScraperToggleBody(BaseModel):
    enabled: bool


@router.post("/plugins/{key}/enabled", operation_id="direct_plugin_set_enabled")
def direct_plugin_set_enabled(
    key: str, body: ScraperToggleBody
) -> PluginsResponse:
    """Switch one scraper on or off, built-in or plugin alike.

    Written to `direct_scraping.disabled` directly rather than through the
    generic settings form -- that field is hidden from the schema for the
    same reason `tailscale.auth_key` is (see `settings/visibility.py`): a
    second write path to the same value with no way to tell which one is
    current is exactly the "two auth key fields" bug repeating itself.
    """

    settings = settings_manager.settings.direct_scraping
    disabled = set(settings.disabled)

    if body.enabled:
        disabled.discard(key)
    else:
        disabled.add(key)

    settings.disabled = sorted(disabled)
    settings_manager.save()
    reset_direct_service()

    return _plugins_response()
