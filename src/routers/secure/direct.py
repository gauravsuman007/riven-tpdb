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
from program.services.directscrapers import DirectScraperService


router = APIRouter(
    responses={404: {"description": "Not found"}},
    prefix="/direct",
    tags=["direct"],
)

# One instance for the process: the scrapers hold a requests.Session each and
# rebuilding them per request would throw away connection pooling and cookies.
_service = DirectScraperService()


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
    limit: Annotated[int, Query(ge=1, le=60)] = 20,
    sites: Annotated[
        str | None, Query(description="Comma-separated site keys")
    ] = None,
) -> DirectSearchResponse:
    search_term = (query or "").strip()

    if not search_term and item_id is not None:
        # db_session is a context manager, not a FastAPI dependency: injecting
        # it hands the route the manager object rather than a Session.
        with db_session() as session:
            item = session.get(MediaItem, item_id)
            if item is None:
                raise HTTPException(status_code=404, detail="Item not found")
            search_term = item.title or ""

    if not search_term:
        raise HTTPException(
            status_code=400, detail="Provide either a query or an item_id"
        )

    selected = [s.strip() for s in sites.split(",")] if sites else None
    results, errors = _service.search(
        search_term, limit_per_site=limit, sites=selected
    )

    return DirectSearchResponse(
        query=search_term,
        results=[
            DirectVideoModel(
                site=video.site,
                site_name=_service.services[video.site].name,
                video_id=video.video_id,
                title=video.title,
                page_url=video.page_url,
                thumbnail=video.thumbnail,
                duration=video.duration,
                resolution=video.resolution,
                size=video.size,
                views=video.views,
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
        sources = _service.resolve(site, video_id)
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
        sources = _service.resolve(site, video_id)
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

    client = httpx.AsyncClient(follow_redirects=True, timeout=30.0)
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
