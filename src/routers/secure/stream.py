import asyncio
import json
import logging
from datetime import datetime
import mimetypes
from typing import Annotated

import httpx

from fastapi import APIRouter, HTTPException, Path, Request, Response
from fastapi.responses import StreamingResponse
from kink import di
from loguru import logger
from pydantic import BaseModel

from program.managers.sse_manager import sse_manager
from program.media.media_entry import MediaEntry
from program.services.streaming import playback_url, transcode
from program.services.streaming.media_stream import PROXY_REQUIRED_PROVIDERS
from program.services.streaming.transcode import PlaybackInfo, SessionManager
from program.services.vpn import STREAMING, VpnUnavailable, vpn
from program.settings import settings_manager
from program.utils.async_client import AsyncClient
from program.utils.proxy_client import ProxyClient

# One manager for the process: HLS sessions are keyed on item id and part, and
# must be shared across requests, which is the whole point of making them
# persistent.
_session_manager = SessionManager()


def _parts_for(item_id: int) -> list["MediaEntry"]:
    """The playable files of `item_id`'s current release, in playing order.

    Raises 404 rather than returning an empty list: every caller here is about
    to play something, and "no parts" is the same condition the single-file
    path already reported as "item has no media file".
    """

    from program.db.db import db_session
    from program.media.item import MediaItem

    with db_session() as session:
        item = session.get(MediaItem, item_id)

        if not item:
            raise HTTPException(status_code=404, detail="Item not found")

        parts = item.media_parts

        if not parts:
            raise HTTPException(status_code=404, detail="Item has no media file")

        # Read everything needed while the session is open. These entries are
        # expunged the moment it closes, and touching a lazy attribute after
        # that raises DetachedInstanceError.
        session.expunge_all()

        return parts

router = APIRouter(
    responses={404: {"description": "Not found"}},
    prefix="/stream",
    tags=["stream"],
)


class SSELogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord):
        log_entry = {
            "time": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "message": record.msg,
        }
        sse_manager.publish_event("logging", json.dumps(log_entry))


logger.add(SSELogHandler())


class EventTypesResponse(BaseModel):
    event_types: list[str]


@router.get(
    "/event_types",
    response_model=EventTypesResponse,
)
async def get_event_types():
    return EventTypesResponse(
        event_types=list(sse_manager.subscribers.keys()),
    )


@router.get("/{event_type}")
async def stream_events(
    event_type: Annotated[
        str,
        Path(
            description="The type of event to stream",
            min_length=1,
        ),
    ],
) -> StreamingResponse:
    return StreamingResponse(
        sse_manager.subscribe(event_type),
        media_type="text/event-stream",
    )


def _get_client(provider: str) -> httpx.AsyncClient:
    """Get the appropriate HTTP client based on provider requirements."""
    use_proxy = (
        provider in PROXY_REQUIRED_PROVIDERS
        and settings_manager.settings.downloaders.proxy_url
    )
    return di[ProxyClient] if use_proxy else di[AsyncClient]


def _build_forward_headers(request: Request) -> dict[str, str]:
    """Build headers to forward to upstream."""
    headers: dict[str, str] = {}
    if "range" in request.headers:
        headers["Range"] = request.headers["range"]
    return headers


def _extract_response_headers(
    upstream_response: httpx.Response,
    filename: str,
) -> dict[str, str]:
    """Extract relevant headers from upstream response."""
    headers: dict[str, str] = {}
    for key in ["content-type", "content-length", "content-range", "accept-ranges"]:
        if key in upstream_response.headers:
            headers[key] = upstream_response.headers[key]
    headers["content-disposition"] = f'inline; filename="{filename}"'
    return headers


@router.get("/file/{item_id}")
async def stream_file(
    item_id: int,
    request: Request,
    part: int = 0,
) -> StreamingResponse:
    """
    Stream a file directly from the provider.

    The URL is resolved through `playback_url` rather than read straight off the
    MediaEntry, and a rejection from the provider triggers exactly one re-mint
    before giving up. Stored links expire, and for providers that mint links per
    request the stored value is not even a fetchable URL -- previously both
    cases surfaced as a flat 502 with no attempt to recover.
    """

    media = playback_url.resolve(item_id, part=part)
    forward_headers = _build_forward_headers(request)

    upstream_response: httpx.Response | None = None

    try:
        for attempt in (0, 1):
            client = _get_client(media.provider)
            req = client.build_request("GET", media.url, headers=forward_headers)

            try:
                upstream_response = await client.send(req, stream=True)
            except httpx.HTTPStatusError as e:
                # AsyncClient raises on 4xx/5xx via an event hook rather than
                # returning the response, so the rejection arrives here.
                status_code = e.response.status_code
                await e.response.aclose()
                upstream_response = None

                if attempt == 0 and status_code < 500:
                    # 400/401/403/404/410 from a debrid CDN all mean the same
                    # thing in practice: this link is spent. Mint a new one.
                    logger.debug(
                        f"Upstream rejected the stored link for item {item_id} "
                        f"({status_code}); re-minting"
                    )
                    media = playback_url.resolve(item_id, force=True, part=part)
                    continue

                raise HTTPException(
                    status_code=502, detail=f"Upstream error: {status_code}"
                )
            except Exception as e:
                # The message can embed the provider URL, token and all.
                logger.error(
                    f"Failed to connect to upstream "
                    f"{playback_url.redact(media.url)}: {playback_url.redact(str(e))}"
                )
                raise HTTPException(status_code=502, detail="Upstream connection failed")

            break

        assert upstream_response is not None

        response_headers = _extract_response_headers(upstream_response, media.filename)

        # Force correct MIME type based on extension.
        # Firefox fails on application/octet-stream, which many providers send.
        guessed_type, _ = mimetypes.guess_type(media.filename)
        if guessed_type:
            response_headers["content-type"] = guessed_type

        async def stream_iterator():
            try:
                async for chunk in upstream_response.aiter_bytes():
                    yield chunk
            except Exception as e:
                logger.error(f"Error during streaming: {e}")
            finally:
                await upstream_response.aclose()

        return StreamingResponse(
            stream_iterator(),
            status_code=upstream_response.status_code,
            headers=response_headers,
            media_type=response_headers.get("content-type"),
        )
    except HTTPException:
        raise
    except Exception as e:
        if upstream_response is not None and not upstream_response.is_closed:
            await upstream_response.aclose()
        logger.exception(f"Unexpected error in stream_file: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


class DirectPlaybackModel(BaseModel):
    """Whether a player may fetch this item straight from the debrid CDN."""

    #: Present only when handing the URL out is both enabled and safe. Absent
    #: means "keep using /stream/file"; it is never an error.
    url: str | None = None
    #: Why not, when there is no url. Shown in logs and the UI, never guessed.
    reason: str | None = None


@router.get("/direct/{item_id}")
def direct_playback(item_id: int, part: int = 0) -> DirectPlaybackModel:
    """The provider URL for one item, when handing it out is safe.

    Proxying video through this server makes its upstream connection the
    ceiling for playback: every byte crosses it once inbound from the provider
    and once outbound to the player, and every seek pays both again. When the
    provider's CDN can serve the player directly, it should.

    Measured against TorBox before this was written: the minted URL is not
    bound to the requesting IP, the CDN reflects `Origin` (so fetch and MSE
    work, not just a plain `<video src>`), and it honours range requests. So
    the mechanism is sound. Each refusal below is a case where it is not.
    """

    settings = settings_manager.settings.stream

    if not settings.direct_debrid_handoff:
        # Off by default on purpose: the provider embeds the account API key
        # in the URL. See the setting's own note.
        return DirectPlaybackModel(
            reason="direct playback is disabled in settings"
        )

    try:
        if vpn().proxy_for(STREAMING) is not None:
            # Handing the URL to a player would quietly take playback off the
            # tunnel -- the exact thing the setting exists to prevent, and
            # invisibly, which is worse than not offering it.
            return DirectPlaybackModel(
                reason="playback is routed through the VPN"
            )
    except VpnUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # `check=True` verifies the link and re-mints a spent one. A player gets a
    # single attempt at this URL and cannot recover from a stale one the way
    # /stream/file does, so it must be known-good before it leaves here.
    media = playback_url.resolve(item_id, check=True, part=part)

    if media.provider in PROXY_REQUIRED_PROVIDERS:
        # These providers bind the link to the fetching client in ways a
        # player cannot satisfy; the proxy exists precisely for them.
        return DirectPlaybackModel(
            reason=f"{media.provider} links must be fetched through this server"
        )

    logger.debug(
        f"Handing item {item_id} directly to the player: "
        f"{playback_url.redact(media.url)}"
    )

    return DirectPlaybackModel(url=media.url)


@router.get("/playback_info/{item_id}")
async def get_playback_info(item_id: int, part: int = 0) -> PlaybackInfo:
    """
    Describe what the file actually contains, so the client can choose a mode.

    The previous player asked the browser whether it supported HEVC and never
    looked at the file, which sent every Firefox viewer down the transcoding
    path regardless of what they were playing. The decision belongs here, on
    real codec data, with the client confirming direct play against its own
    canPlayType.
    """

    media = playback_url.resolve(item_id, check=True, part=part)
    result = transcode.probe(media.url, cache_key=media.filename)
    mode, reason = transcode.decide(result)

    return PlaybackInfo(
        item_id=item_id,
        probe=result,
        mode=mode,
        mime_type=transcode._mime_for(result),
        reason=reason,
        file_size=media.file_size or None,
    )


@router.get("/remux/{item_id}")
async def stream_remux(item_id: int, t: float = 0.0, part: int = 0) -> StreamingResponse:
    """
    Progressive fragmented-MP4 remux for files whose video is already playable.

    Only the audio is re-encoded and the container is rebuilt, so this costs a
    fraction of a full transcode. `t` seeks, since a fragmented stream cannot be
    range-requested.
    """

    media = playback_url.resolve(item_id, check=True, part=part)
    cmd = transcode.build_remux_command(media.url, start_time=t)

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def pump():
        try:
            assert process.stdout

            while chunk := await process.stdout.read(64 * 1024):
                yield chunk
        finally:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass

            if process.returncode not in (0, None) and process.stderr:
                error = (await process.stderr.read()).decode(errors="replace")
                logger.error(f"Remux failed for item {item_id}: {error.strip()[:400]}")

    return StreamingResponse(pump(), media_type="video/mp4")


@router.get("/hls/{item_id}/index.m3u8")
async def get_hls_playlist(item_id: int, part: int = 0):
    """
    A static VOD playlist derived from the file's real duration.

    Segments are a fixed length because the encoder is told to force keyframes
    at exactly those boundaries, so what this advertises is what the session
    actually produces.
    """

    media = playback_url.resolve(item_id, check=True, part=part)
    result = transcode.probe(media.url, cache_key=media.filename)

    return Response(
        content=transcode.build_playlist(result.duration),
        media_type="application/vnd.apple.mpegurl",
        headers={"cache-control": "no-store"},
    )


@router.get("/hls/{item_id}/segment/{seq}.ts")
async def get_hls_segment(item_id: int, seq: int, part: int = 0) -> Response:
    """
    Serve one segment from the item's running transcode session.

    The session is persistent: the old implementation started a new ffmpeg for
    every segment, each one re-opening the remote debrid URL and seeking from
    the beginning of the file.
    """

    if seq < 0:
        raise HTTPException(status_code=400, detail="Invalid segment")

    media = playback_url.resolve(item_id, check=True, part=part)
    result = transcode.probe(media.url, cache_key=media.filename)

    data = await _session_manager.segment(
        # Per PART, not per item: two parts of one release transcoding at
        # once are two different files, and sharing a session would serve
        # segments of one under the other's playlist.
        session_key=f"{item_id}:{part}",
        seq=seq,
        url=media.url,
        # Video is always re-encoded in HLS mode -- see the note in
        # program.services.streaming.transcode on why copy cannot be used here.
        copy_video=False,
        copy_audio=result.audio_playable and result.audio_codec == "aac",
    )

    if data is None:
        raise HTTPException(status_code=503, detail="Transcoder produced no segment")

    return Response(
        content=data,
        media_type="video/mp2t",
        headers={"cache-control": "no-store"},
    )


@router.delete("/hls/{item_id}")
async def stop_hls_session(item_id: int, part: int | None = None) -> dict[str, bool]:
    """Tear down an item's session when the player closes.

    With no `part`, every part's session goes: closing the player should not
    leave a transcode running for a part the viewer moved off earlier.
    """

    if part is None:
        for index in range(len(_parts_for(item_id))):
            await _session_manager.stop(f"{item_id}:{index}")
    else:
        await _session_manager.stop(f"{item_id}:{part}")

    return {"success": True}


class MediaPart(BaseModel):
    """One playable file of a multi-file release."""

    index: Annotated[int, "Position in the playlist; what `?part=` takes."]
    title: str
    """The filename with its extension and separators cleaned up. These
    releases name their files after the scene or the performer, which is the
    only description of a part that exists -- there is no per-file metadata to
    read and inventing "Part 1" would throw away the one real label."""
    filename: str
    file_size: int
    duration: float | None = None
    """Seconds, only when the file has already been probed. Never probed here:
    a six-part release would mean six ffprobe runs against remote URLs before
    the player could draw a list."""


class MediaPartsResponse(BaseModel):
    item_id: int
    title: str
    parts: list[MediaPart]
    """Always at least one entry. A single-file title is a one-part playlist,
    so the client has one shape to handle rather than two."""


def _part_title(filename: str) -> str:
    """A human label for a part, from its filename.

    Extension off, separators to spaces, collapsed. Deliberately not clever:
    the file is called "Kristen Scott 2.mp4" and "Kristen Scott 2" is the best
    possible name for that part.
    """

    stem = filename.rsplit("/", 1)[-1]
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    cleaned = stem.replace("_", " ").replace(".", " ").strip()

    return " ".join(cleaned.split()) or filename


@router.get("/parts/{item_id}", operation_id="get_media_parts")
def get_media_parts(item_id: int) -> MediaPartsResponse:
    """Every playable file of one title, in playing order.

    A scene compilation is one torrent holding five or six separate scenes.
    Playback resolved a single file and called that the title, so the rest
    were downloaded and unreachable. This is what makes them addressable: the
    player builds a playlist from it and each entry plays through `?part=N`.
    """

    from program.db.db import db_session
    from program.media.item import MediaItem

    parts = _parts_for(item_id)

    with db_session() as session:
        item = session.get(MediaItem, item_id)
        title = item.title if item else ""

    return MediaPartsResponse(
        item_id=item_id,
        title=title or "",
        parts=[
            MediaPart(
                index=index,
                title=_part_title(entry.original_filename),
                filename=entry.original_filename,
                file_size=entry.file_size or 0,
                duration=(
                    entry.media_metadata.duration
                    if entry.media_metadata is not None
                    else None
                ),
            )
            for index, entry in enumerate(parts)
        ],
    )


@router.get("/playlist/{item_id}.m3u", operation_id="get_media_playlist")
def get_media_playlist(item_id: int, request: Request) -> Response:
    """The title as an M3U playlist, for players that are not this app.

    An external player gets handed a URL and nothing else -- there is no way
    to tell VLC or MX Player "and then five more files". A playlist is that
    way, and M3U is the one format all of them read.

    URLs are absolute and built from the request, so the file works in
    whatever app opens it: a relative path would resolve against the player's
    own idea of a base and fetch nothing.
    """

    parts = _parts_for(item_id)
    base = str(request.base_url).rstrip("/")
    api_key = request.query_params.get("api_key")

    lines = ["#EXTM3U"]

    for index, entry in enumerate(parts):
        duration = -1

        if entry.media_metadata is not None and entry.media_metadata.duration:
            duration = int(entry.media_metadata.duration)

        url = f"{base}/api/v1/stream/file/{item_id}?part={index}"

        # Carried through when the caller authenticated that way. An external
        # player has no session and no header to send, so a playlist whose
        # entries drop the key is a playlist of 401s.
        if api_key:
            url += f"&api_key={api_key}"

        lines.append(f"#EXTINF:{duration},{_part_title(entry.original_filename)}")
        lines.append(url)

    return Response(
        content="\n".join(lines) + "\n",
        media_type="audio/x-mpegurl",
        headers={
            "content-disposition": f'inline; filename="item-{item_id}.m3u"',
            "cache-control": "no-store",
        },
    )
