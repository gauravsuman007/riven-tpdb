"""The OnlyFans performer index, and live content for one performer.

Two different kinds of data behind one prefix, and as with the studio
directory the difference is the design:

    * Accounts are mirrored locally and served from the database. Building the
      list means crawling five sites' model indexes, so it happens weekly and
      the page reads the result.
    * An account's *content* is read live from the sites on every request.
      Videos and galleries are paged straight out of the site, never stored.
      A performer's feed changes constantly and a copy taken last Sunday is
      not the feed.

Content requests deliberately bypass `DirectScraperService.search`. That path
exists to find one known title across many sites and filters everything it
gets through `best_matches()`, which scores title relevance against a
`MatchTarget`. An account browse has no target -- the question is "what does
this site hold for this person", not "which of these is the film I named" --
so routing it through the ranker would discard almost every result and look
like five broken scrapers.

Images are addressed by position rather than by URL. Accepting a URL to proxy
would make this an open proxy for anything on the internet, and signing them
only moves the problem; resolving the gallery and taking the Nth image cannot
be pointed at a host this app did not choose.
"""

import shutil
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import APIRouter, Body, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from program.db.db import db_session
from program.media.onlyfans import (
    OnlyFansAccount,
    OnlyFansAccountSource,
    OnlyFansSyncRun,
)
from program.services.directscrapers.base import BROWSER_HEADERS
from program.services.onlyfans import OnlyFansService, normalise_handle
from program.services.onlyfans.service import STALE_AFTER
from program.services.onlyfans import registry as of_registry
from program.services.onlyfans import reset as reset_of_registry
from program.services.vpn import STREAMING, VpnUnavailable, vpn
from program.settings import settings_manager
from program.utils.time import utcnow


router = APIRouter(prefix="/onlyfans", tags=["onlyfans"])


_service: "OnlyFansService | None" = None


def service() -> OnlyFansService:
    """The shared index service, or a 503 if the feature is switched off."""

    global _service

    if _service is None or not _service.initialized:
        _service = OnlyFansService()

    if not _service.initialized:
        raise HTTPException(
            status_code=503,
            detail="The OnlyFans performer index is not enabled",
        )

    return _service


# --- Response models --------------------------------------------------------


class AccountSourceResponse(BaseModel):
    site: str
    site_handle: str
    page_url: str
    video_count: int | None
    image_count: int | None


class AccountResponse(BaseModel):
    handle: str
    display_name: str
    avatar_url: str | None
    bio: str | None
    source_count: int
    saved: bool
    sites: list[str]


class AccountDetailResponse(AccountResponse):
    sources: list[AccountSourceResponse]


class AccountPage(BaseModel):
    """A page of accounts plus the total, so the grid can stop asking.

    `total` is the count *matching the filter*, not the table size -- an
    infinite scroll that keeps requesting because it compared against the
    unfiltered count would never terminate on a search.
    """

    items: list[AccountResponse]
    total: int
    offset: int
    limit: int


class VideoResponse(BaseModel):
    site: str
    video_id: str
    title: str
    page_url: str
    thumbnail: str | None
    duration: int | None
    resolution: str | None
    views: int | None
    hd: bool


class GalleryResponse(BaseModel):
    site: str
    gallery_id: str
    title: str
    page_url: str
    cover: str | None
    image_count: int | None
    posted: str | None


class GalleryImageResponse(BaseModel):
    """One image, addressed by position rather than by URL.

    `index` is what `/image` takes. The URL itself is not handed to the
    browser: it carries a short-lived token and these hosts check Referer, so
    a raw `<img src>` would 403 or expire.
    """

    index: int
    width: int | None
    height: int | None


def _account_response(account: OnlyFansAccount) -> AccountResponse:
    return AccountResponse(
        handle=account.handle,
        display_name=account.display_name,
        avatar_url=account.avatar_url,
        bio=account.bio,
        source_count=account.source_count,
        saved=account.saved,
        sites=[source.site for source in account.sources],
    )


# --- The index --------------------------------------------------------------


@router.get("/accounts", operation_id="list_onlyfans_accounts")
def list_accounts(
    search: Annotated[str | None, Query()] = None,
    saved: Annotated[bool | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 60,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AccountPage:
    """A page of the performer index.

    Real offset paging rather than the studio directory's bare `limit`: that
    list is ~1,200 rows and can be sent whole, this one runs to tens of
    thousands across five sites.

    The search matches the *collapsed* handle as well as the display name, so
    typing "sophie rain", "sophierain" or "Sophie-Rain" all find the same
    account -- which is the whole reason the collapsed form is stored.
    """

    with db_session() as session:
        query = select(OnlyFansAccount)

        if saved is not None:
            query = query.where(OnlyFansAccount.saved.is_(saved))

        if search and search.strip():
            collapsed = normalise_handle(search)
            query = query.where(
                or_(
                    OnlyFansAccount.handle.like(f"%{collapsed}%"),
                    OnlyFansAccount.display_name.ilike(f"%{search.strip()}%"),
                )
            )

        total = session.execute(
            select(func.count()).select_from(query.subquery())
        ).scalar_one()

        accounts = (
            session.execute(
                # Accounts several sites agree on first: one that three
                # independent archives indexed is likelier to be a real,
                # findable performer than one that appears once.
                query.order_by(
                    OnlyFansAccount.source_count.desc(),
                    OnlyFansAccount.display_name.asc(),
                )
                .offset(offset)
                .limit(limit)
            )
            .scalars()
            .all()
        )

        return AccountPage(
            items=[_account_response(account) for account in accounts],
            total=total,
            offset=offset,
            limit=limit,
        )


@router.get("/accounts/{handle}", operation_id="get_onlyfans_account")
def get_account(handle: str) -> AccountDetailResponse:
    """One account and every site that carries it.

    The sources are what the detail page's per-site buttons are built from:
    each one names the scraper key to ask and the slug to ask it for.
    """

    with db_session() as session:
        account = _lookup(session, handle)

        return AccountDetailResponse(
            **_account_response(account).model_dump(),
            sources=[
                AccountSourceResponse(
                    site=source.site,
                    site_handle=source.site_handle,
                    page_url=source.page_url,
                    video_count=source.video_count,
                    image_count=source.image_count,
                )
                for source in account.sources
            ],
        )


@router.post("/accounts/{handle}/save", operation_id="save_onlyfans_account")
def save_account(handle: str) -> AccountResponse:
    return _set_saved(handle, True)


@router.delete("/accounts/{handle}/save", operation_id="unsave_onlyfans_account")
def unsave_account(handle: str) -> AccountResponse:
    return _set_saved(handle, False)


def _set_saved(handle: str, saved: bool) -> AccountResponse:
    with db_session() as session:
        account = _lookup(session, handle)
        account.saved = saved
        account.saved_at = utcnow() if saved else None
        session.commit()
        return _account_response(account)


def _lookup(session, handle: str) -> OnlyFansAccount:
    """An account by handle, collapsed on the way in.

    Collapsing means a link built from a display name still resolves, rather
    than 404ing on the difference between "Sophie Rain" and "sophierain".
    """

    account = session.execute(
        select(OnlyFansAccount).where(
            OnlyFansAccount.handle == normalise_handle(handle)
        )
    ).scalar_one_or_none()

    if account is None:
        raise HTTPException(status_code=404, detail="No such account")

    return account


# --- Live content -----------------------------------------------------------


def _scraper_for(handle: str, site: str):
    """The plugin for `site`, and this account's slug on it.

    Both halves matter: the slug is the site's own and is not derivable from
    the collapsed handle, so an account known to this site under a different
    spelling would otherwise 404 on a URL built from the wrong one.
    """

    with db_session() as session:
        account = _lookup(session, handle)
        source = next(
            (item for item in account.sources if item.site == site), None
        )

        if source is None:
            raise HTTPException(
                status_code=404, detail=f"{site} does not carry this account"
            )
        site_handle = source.site_handle

    scraper = of_registry().services.get(site)

    if scraper is None:
        raise HTTPException(
            status_code=404, detail=f"No scraper named {site} is installed"
        )

    return scraper, site_handle


@router.get("/accounts/{handle}/videos", operation_id="get_onlyfans_account_videos")
def account_videos(
    handle: str,
    site: Annotated[str, Query()],
    page: Annotated[int, Query(ge=1)] = 1,
) -> list[VideoResponse]:
    """One page of an account's videos on one site, newest first.

    An empty list is a valid answer and means the site has no more, which is
    how the infinite scroll knows to stop. A site that is down raises 502 so
    the page can say so rather than showing the same empty grid.
    """

    scraper, site_handle = _scraper_for(handle, site)

    try:
        videos = scraper.account_videos(site_handle, page)
    except Exception as exc:
        logger.warning(f"OnlyFans: {site} videos failed for {handle}: {exc}")
        raise HTTPException(
            status_code=502, detail=f"{site} could not be read"
        ) from exc

    return [
        VideoResponse(
            site=video.site,
            video_id=video.video_id,
            title=video.title,
            page_url=video.page_url,
            thumbnail=video.thumbnail,
            duration=video.duration,
            resolution=video.resolution,
            views=video.views,
            hd=video.hd,
        )
        for video in videos
    ]


@router.get(
    "/accounts/{handle}/galleries", operation_id="get_onlyfans_account_galleries"
)
def account_galleries(
    handle: str,
    site: Annotated[str, Query()],
    page: Annotated[int, Query(ge=1)] = 1,
) -> list[GalleryResponse]:
    """One page of an account's image galleries on one site.

    Most of this family returns nothing here, and that is the site's answer
    rather than a failure: only one of the five attributes galleries to a
    performer at all. See each scraper's module docstring.
    """

    scraper, site_handle = _scraper_for(handle, site)

    try:
        galleries = scraper.account_galleries(site_handle, page)
    except Exception as exc:
        logger.warning(f"OnlyFans: {site} galleries failed for {handle}: {exc}")
        raise HTTPException(
            status_code=502, detail=f"{site} could not be read"
        ) from exc

    return [
        GalleryResponse(
            site=gallery.site,
            gallery_id=gallery.gallery_id,
            title=gallery.title,
            page_url=gallery.page_url,
            cover=gallery.cover,
            image_count=gallery.image_count,
            posted=gallery.posted,
        )
        for gallery in galleries
    ]


#: Resolved galleries, keyed on (site, gallery_id). A lightbox opens N images
#: from one gallery and each `/image` call would otherwise re-fetch and
#: re-parse the album page. Short-lived because the image URLs carry tokens
#: that expire -- caching them for longer would serve 403s from memory.
_GALLERY_TTL = 120.0
_gallery_cache: dict[tuple[str, str], tuple[float, list]] = {}


def _gallery_images(site: str, gallery_id: str) -> list:
    cached = _gallery_cache.get((site, gallery_id))

    if cached and (time.monotonic() - cached[0]) < _GALLERY_TTL:
        return cached[1]

    scraper = of_registry().services.get(site)

    if scraper is None:
        raise HTTPException(
            status_code=404, detail=f"No scraper named {site} is installed"
        )

    try:
        images = scraper.gallery_images(gallery_id)
    except Exception as exc:
        logger.warning(f"OnlyFans: {site} gallery {gallery_id} failed: {exc}")
        raise HTTPException(
            status_code=502, detail=f"{site} could not be read"
        ) from exc

    _gallery_cache[(site, gallery_id)] = (time.monotonic(), images)
    return images


@router.get("/galleries/{site}/{gallery_id}", operation_id="get_onlyfans_gallery")
def gallery(site: str, gallery_id: str) -> list[GalleryImageResponse]:
    """Every image in one gallery, by position.

    Positions, not URLs -- see the module docstring. The count returned here
    can be well short of the gallery's advertised size: these sites show a
    signed-out visitor a handful of images from an album and gate the rest.
    """

    images = _gallery_images(site, gallery_id)

    return [
        GalleryImageResponse(index=index, width=image.width, height=image.height)
        for index, image in enumerate(images)
    ]


@router.get("/image", operation_id="get_onlyfans_image")
async def image(
    site: Annotated[str, Query()],
    gallery_id: Annotated[str, Query()],
    index: Annotated[int, Query(ge=0)] = 0,
) -> StreamingResponse:
    """Proxy one image out of a gallery.

    Proxied rather than linked for the same reason `/direct/stream` proxies
    video: the URL carries a short-lived token and the host checks Referer, so
    a browser asked to load it directly gets a 403 or an expired link.
    """

    images = _gallery_images(site, gallery_id)

    if index >= len(images):
        raise HTTPException(status_code=404, detail="No such image")

    source = images[index]

    try:
        proxy = vpn().proxy_for(STREAMING)
    except VpnUnavailable as exc:
        # Not falling back to a direct connection, for the same reason
        # `/direct/stream` does not: someone routing playback is controlling
        # where it appears to come from, and quietly using the host's own
        # address would defeat the setting invisibly.
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    client = httpx.AsyncClient(follow_redirects=True, timeout=30.0, proxy=proxy)

    try:
        upstream = await client.send(
            client.build_request(
                "GET", source.url, headers={**BROWSER_HEADERS, **source.headers}
            ),
            stream=True,
        )
    except Exception as exc:
        await client.aclose()
        logger.error(f"OnlyFans image upstream failed for {site}:{gallery_id}: {exc}")
        raise HTTPException(status_code=502, detail="Upstream connection failed")

    if upstream.status_code >= 400:
        status_code = upstream.status_code
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"Upstream returned {status_code}")

    headers = {
        key: upstream.headers[key]
        for key in ("content-type", "content-length")
        if key in upstream.headers
    }
    # These are immutable once published and the token in the URL is what
    # expires, not the bytes, so the browser may keep them for the session.
    headers["cache-control"] = "private, max-age=3600"

    async def body():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        except Exception as exc:
            logger.debug(f"OnlyFans image interrupted for {site}:{gallery_id}: {exc}")
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(body(), status_code=upstream.status_code, headers=headers)


class SyncRunResponse(BaseModel):
    """One site's index walk, as it stands."""

    site: str
    #: "running", "ok", "failed", or "never" for a site that has not been
    #: walked since this table existed.
    state: str
    started_at: str | None = None
    finished_at: str | None = None
    pages: int = 0
    accounts_seen: int = 0
    accounts_new: int = 0
    error: str | None = None
    #: True when the scraper is present and says it indexes accounts. A site
    #: that cannot be walked is shown saying so rather than sitting at
    #: "never" looking like it is merely waiting its turn.
    available: bool = True


class SyncStatusResponse(BaseModel):
    running: bool
    sites: list[SyncRunResponse]
    #: Index totals, so the page can say "3,806 accounts, 2,140 with a
    #: picture" without a second request.
    accounts: int = 0
    accounts_with_avatar: int = 0


#: Sites with a walk in flight. A second request for a site already running is
#: refused rather than queued: two walks of one index race on the same rows
#: for no benefit, and the status row can only describe one of them.
_running: set[str] = set()
_running_lock = threading.Lock()


def _run_sync(sites: list[str]) -> None:
    try:
        service().sync(sites=sites)
    except Exception as exc:
        logger.error(f"OnlyFans: manual sync failed: {exc}")
    finally:
        with _running_lock:
            _running.difference_update(sites)


@router.post("/sync", operation_id="sync_onlyfans_accounts")
def sync(sites: Annotated[list[str] | None, Query()] = None) -> SyncStatusResponse:
    """Start an index walk now, rather than waiting for the weekly job.

    Returns immediately with the status rather than the result. A full walk of
    all five sites is several hundred requests and minutes of work; holding
    the connection open for it would time out in every proxy between here and
    the browser, and would give no progress while it ran. `GET /sync/status`
    is the readout.
    """

    configured = list(settings_manager.settings.onlyfans.sites)
    wanted = [key for key in (sites or configured) if key in configured]

    if not wanted:
        raise HTTPException(
            status_code=400, detail="No configured OnlyFans site was named"
        )

    with _running_lock:
        already = sorted(_running.intersection(wanted))

        if already:
            raise HTTPException(
                status_code=409,
                detail=f"Already indexing {', '.join(already)}",
            )

        _running.update(wanted)

    # A thread, not a task queue: this is one long IO-bound walk with no
    # ordering requirement and nothing to hand back, and the scheduler that
    # owns the weekly run is not a general-purpose executor.
    threading.Thread(
        target=_run_sync, args=(wanted,), name="onlyfans-sync", daemon=True
    ).start()

    return _sync_status()


def _sync_status() -> SyncStatusResponse:
    settings = settings_manager.settings.onlyfans
    installed = of_registry().services

    with db_session() as session:
        runs = {
            run.site: run
            for run in session.execute(select(OnlyFansSyncRun)).scalars().all()
        }
        accounts = session.execute(
            select(func.count()).select_from(OnlyFansAccount)
        ).scalar_one()
        with_avatar = session.execute(
            select(func.count())
            .select_from(OnlyFansAccount)
            .where(OnlyFansAccount.avatar_url.is_not(None))
        ).scalar_one()

        rows: list[SyncRunResponse] = []

        for site in settings.sites:
            run = runs.get(site)
            scraper = installed.get(site)
            available = scraper is not None and getattr(
                scraper, "indexes_accounts", False
            )

            if run is None:
                rows.append(SyncRunResponse(site=site, state="never", available=available))
                continue

            state = run.state

            # A run left as `running` by a restart has no process behind it
            # and nothing will ever finish it, so it is reported as failed
            # rather than as a progress bar that never moves.
            if state == "running" and run.started_at is not None:
                age = (utcnow() - run.started_at).total_seconds()
                if age > STALE_AFTER and site not in _running:
                    state = "failed"

            rows.append(
                SyncRunResponse(
                    site=site,
                    state=state,
                    started_at=run.started_at.isoformat() if run.started_at else None,
                    finished_at=run.finished_at.isoformat() if run.finished_at else None,
                    pages=run.pages,
                    accounts_seen=run.accounts_seen,
                    accounts_new=run.accounts_new,
                    error=run.error
                    if state != "failed" or run.error
                    else "interrupted before it finished",
                    available=available,
                )
            )

    return SyncStatusResponse(
        running=bool(_running),
        sites=rows,
        accounts=accounts,
        accounts_with_avatar=with_avatar,
    )


@router.get("/sync/status", operation_id="onlyfans_sync_status")
def sync_status() -> SyncStatusResponse:
    """Per-site progress, cheap enough to poll while a walk is running."""

    return _sync_status()


# --- Playback ---------------------------------------------------------------
#
# Mirrors /direct/sources|handoff|stream rather than reusing them. Those look
# the site up in the direct-play registry, which by design does not contain
# these scrapers, so an account video played through them would 404. The
# duplication is the cost of keeping the two sets genuinely separate.


class SourceResponse(BaseModel):
    """One rendition, without its URL.

    The URL is deliberately withheld: it expires and several of these hosts
    check Referer, so handing it to the browser produces a link that 403s on
    use. `index` is what /stream and /handoff take.
    """

    index: int
    label: str
    resolution: str | None
    size: int | None
    mime_type: str


class SourcesResponse(BaseModel):
    site: str
    video_id: str
    sources: list[SourceResponse]


class HandoffResponse(BaseModel):
    """Whether a player can be pointed straight at the CDN.

    `reason` is filled in instead of `url` when it cannot, so the caller can
    fall back to the proxy rather than guessing why it got nothing.
    """

    url: str | None = None
    mime_type: str | None = None
    reason: str | None = None


def _resolve(site: str, video_id: str) -> list:
    scraper = of_registry().services.get(site)

    if scraper is None:
        raise HTTPException(
            status_code=404, detail=f"No scraper named {site} is installed"
        )

    try:
        sources = scraper.resolve(video_id)
    except Exception as exc:
        logger.warning(f"OnlyFans resolve failed for {site}:{video_id}: {exc}")
        raise HTTPException(
            status_code=502, detail="Could not resolve this video"
        ) from exc

    if not sources:
        raise HTTPException(status_code=404, detail="No playable source")

    return sources


@router.get("/sources", operation_id="onlyfans_sources")
def sources(
    site: Annotated[str, Query()],
    video_id: Annotated[str, Query()],
) -> SourcesResponse:
    """Every rendition of one account video, best quality first."""

    resolved = _resolve(site, video_id)

    return SourcesResponse(
        site=site,
        video_id=video_id,
        sources=[
            SourceResponse(
                index=index,
                label=source.label,
                resolution=source.resolution,
                size=source.size,
                mime_type=source.mime_type,
            )
            for index, source in enumerate(resolved)
        ],
    )


@router.get("/handoff", operation_id="onlyfans_handoff")
def handoff(
    site: Annotated[str, Query()],
    video_id: Annotated[str, Query()],
    index: Annotated[int, Query(ge=0)] = 0,
) -> HandoffResponse:
    """The CDN URL itself, when a player can actually use it.

    Refused rather than returned when the source needs headers a media player
    will not send, or when playback is routed through the VPN -- handing the
    URL over in either case produces a silent failure in the player instead of
    an explanation here.
    """

    resolved = _resolve(site, video_id)

    if index >= len(resolved):
        raise HTTPException(status_code=404, detail="No such source")

    source = resolved[index]

    if source.headers:
        return HandoffResponse(
            reason="the source requires headers a media player will not send"
        )

    try:
        if vpn().proxy_for(STREAMING) is not None:
            return HandoffResponse(reason="playback is routed through the VPN")
    except VpnUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return HandoffResponse(url=source.url, mime_type=source.mime_type)


@router.get("/stream", operation_id="onlyfans_stream")
async def stream(
    request: Request,
    site: Annotated[str, Query()],
    video_id: Annotated[str, Query()],
    index: Annotated[int, Query(ge=0)] = 0,
) -> StreamingResponse:
    """Resolve and proxy one rendition, passing Range through both ways."""

    resolved = _resolve(site, video_id)

    if index >= len(resolved):
        raise HTTPException(status_code=404, detail="No such source")

    source = resolved[index]
    # A browser's User-Agent underneath, for the same reason `/direct/stream`
    # sends one: these hosts serve different responses to anything that looks
    # automated, so a URL the scraper resolved happily can still be refused
    # when the proxy fetches it. The source's own headers win on conflict.
    headers = {**BROWSER_HEADERS, **source.headers}

    if "range" in request.headers:
        headers["Range"] = request.headers["range"]

    try:
        proxy = vpn().proxy_for(STREAMING)
    except VpnUnavailable as exc:
        # Not falling back to a direct connection: someone routing playback is
        # controlling where it appears to come from, and quietly using the
        # host's own address would defeat that invisibly, mid-play.
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    client = httpx.AsyncClient(follow_redirects=True, timeout=30.0, proxy=proxy)

    try:
        upstream = await client.send(
            client.build_request("GET", source.url, headers=headers), stream=True
        )
    except Exception as exc:
        await client.aclose()
        logger.error(f"OnlyFans stream upstream failed for {site}:{video_id}: {exc}")
        raise HTTPException(status_code=502, detail="Upstream connection failed")

    if upstream.status_code >= 400:
        status_code = upstream.status_code
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"Upstream returned {status_code}")

    response_headers = {
        key: upstream.headers[key]
        for key in ("content-type", "content-length", "content-range")
        if key in upstream.headers
    }
    # Advertised unconditionally: these upstreams honour Range, and without it
    # the browser will not offer a seek bar on a fresh stream.
    response_headers["accept-ranges"] = "bytes"

    async def body():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        except Exception as exc:
            logger.debug(f"OnlyFans stream interrupted for {site}:{video_id}: {exc}")
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        body(), status_code=upstream.status_code, headers=response_headers
    )


# --- Scraper management -----------------------------------------------------


class ScraperInfoResponse(BaseModel):
    key: str
    name: str
    base_url: str
    enabled: bool
    source_file: str
    indexes_accounts: bool


class PluginsResponse(BaseModel):
    plugin_dir: str
    scrapers: list[ScraperInfoResponse]
    #: Filename -> what went wrong. Surfaced rather than swallowed so a file
    #: that fails to import reads as broken instead of missing.
    errors: dict[str, str]


class ImportResult(BaseModel):
    filename: str
    accepted: bool
    key: str | None = None
    error: str | None = None


class ImportResponse(BaseModel):
    results: list[ImportResult]
    plugins: PluginsResponse


def _plugins_response() -> PluginsResponse:
    current = of_registry()

    return PluginsResponse(
        plugin_dir=current.plugin_dir,
        # `asdict`, not `vars`: ScraperInfo is a slots dataclass and so has no
        # __dict__ at all, which vars() raises on rather than returning empty.
        scrapers=[ScraperInfoResponse(**asdict(info)) for info in current.describe()],
        errors=current.errors,
    )


@router.get("/plugins", operation_id="onlyfans_plugins")
def plugins() -> PluginsResponse:
    """Every scraper in the OnlyFans folder, disabled ones included."""

    return _plugins_response()


@router.post("/plugins/rescan", operation_id="onlyfans_plugins_rescan")
def rescan() -> PluginsResponse:
    """Re-read the folder, picking up files added or edited on disk."""

    reset_of_registry()
    return _plugins_response()


@router.post("/plugins/{key}/enabled", operation_id="onlyfans_plugin_set_enabled")
def set_enabled(key: str, enabled: Annotated[bool, Body(embed=True)]) -> PluginsResponse:
    """Enable or disable one scraper.

    Disabling leaves the file in place and records the key, so re-enabling does
    not mean re-importing -- and so a scraper disabled because a site broke
    comes back with one click when it is fixed.
    """

    settings = settings_manager.settings.onlyfans
    disabled = set(settings.disabled)

    if enabled:
        disabled.discard(key)
    else:
        disabled.add(key)

    settings.disabled = sorted(disabled)
    settings_manager.save()
    reset_of_registry()

    return _plugins_response()


#: Uploads are written here first and only moved into the plugin folder once
#: they load. A file that fails validation never reaches the folder, so it
#: cannot become a permanent error row that someone has to clean up by hand.
_MAX_PLUGIN_BYTES = 1024 * 1024


@router.post("/plugins/import", operation_id="onlyfans_plugins_import")
async def import_plugins(
    files: Annotated[list[UploadFile], File()],
) -> ImportResponse:
    """Add scraper files to the OnlyFans folder.

    Each file is validated before it is kept: written to a temporary folder,
    loaded through the same discovery the app uses, and moved in only if it
    actually yields a `DirectScraper`. Rejecting up front is the difference
    between "that file was not a scraper" and a permanent broken row in the
    tab that nobody can explain.
    """

    target = Path(of_registry().plugin_dir)

    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail=f"Plugin folder is not writable: {exc}"
        ) from exc

    results: list[ImportResult] = []

    for upload in files:
        # `Path(...).name` rather than trusting the client: a filename carrying
        # a path separator would otherwise write outside the plugin folder.
        name = Path(upload.filename or "").name

        if not name.endswith(".py") or name.startswith((".", "_")):
            results.append(
                ImportResult(
                    filename=name or "(unnamed)",
                    accepted=False,
                    error="Not a plugin file: expected a .py file",
                )
            )
            continue

        payload = await upload.read()

        if len(payload) > _MAX_PLUGIN_BYTES:
            results.append(
                ImportResult(
                    filename=name, accepted=False, error="File is too large"
                )
            )
            continue

        with tempfile.TemporaryDirectory() as staging:
            staged = Path(staging) / name
            staged.write_bytes(payload)

            from program.services.directscrapers.plugins import discover_plugins

            found = discover_plugins(staging)

            if found.errors:
                results.append(
                    ImportResult(
                        filename=name,
                        accepted=False,
                        error=next(iter(found.errors.values())),
                    )
                )
                continue

            if not found.plugins:
                results.append(
                    ImportResult(
                        filename=name,
                        accepted=False,
                        error="File defines no DirectScraper subclass",
                    )
                )
                continue

            key = next(iter(found.plugins))

            try:
                shutil.move(str(staged), target / name)
            except OSError as exc:
                results.append(
                    ImportResult(filename=name, accepted=False, error=str(exc))
                )
                continue

            results.append(ImportResult(filename=name, accepted=True, key=key))

    reset_of_registry()
    return ImportResponse(results=results, plugins=_plugins_response())
