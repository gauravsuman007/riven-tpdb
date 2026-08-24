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
from program.services.streaming import playback_url, transcode
from program.services.streaming.media_stream import PROXY_REQUIRED_PROVIDERS
from program.services.streaming.transcode import PlaybackInfo, SessionManager
from program.settings import settings_manager
from program.utils.async_client import AsyncClient
from program.utils.proxy_client import ProxyClient

# One manager for the process: HLS sessions are keyed on item id and must be
# shared across requests, which is the whole point of making them persistent.
_session_manager = SessionManager()

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
) -> StreamingResponse:
    """
    Stream a file directly from the provider.

    The URL is resolved through `playback_url` rather than read straight off the
    MediaEntry, and a rejection from the provider triggers exactly one re-mint
    before giving up. Stored links expire, and for providers that mint links per
    request the stored value is not even a fetchable URL -- previously both
    cases surfaced as a flat 502 with no attempt to recover.
    """

    media = playback_url.resolve(item_id)
    forward_headers = _build_forward_headers(request)

    upstream_response: httpx.Response | None = None

    try:
        for attempt in (0, 1):
            client = _get_client(media.provider)
            req = client.build_request("GET", media.url, headers=forward_headers)

            try:
                upstream_response = await client.send(req, stream=True)
            except Exception as e:
                logger.error(
                    f"Failed to connect to upstream {playback_url.redact(media.url)}: {e}"
                )
                raise HTTPException(status_code=502, detail="Upstream connection failed")

            if upstream_response.status_code < 400:
                break

            await upstream_response.aclose()

            if attempt == 0:
                # 400/401/403/404/410 from a debrid CDN all mean the same thing
                # in practice: this link is spent. Mint a new one and retry.
                logger.debug(
                    f"Upstream rejected the stored link for item {item_id} "
                    f"({upstream_response.status_code}); re-minting"
                )
                media = playback_url.resolve(item_id, force=True)
                continue

            raise HTTPException(
                status_code=502,
                detail=f"Upstream error: {upstream_response.status_code}",
            )

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


@router.get("/playback_info/{item_id}")
async def get_playback_info(item_id: int) -> PlaybackInfo:
    """
    Describe what the file actually contains, so the client can choose a mode.

    The previous player asked the browser whether it supported HEVC and never
    looked at the file, which sent every Firefox viewer down the transcoding
    path regardless of what they were playing. The decision belongs here, on
    real codec data, with the client confirming direct play against its own
    canPlayType.
    """

    media = playback_url.resolve(item_id)
    result = transcode.probe(media.url, cache_key=media.filename)
    mode, reason = transcode.decide(result)

    return PlaybackInfo(
        item_id=item_id,
        probe=result,
        mode=mode,
        mime_type=transcode._mime_for(result),
        reason=reason,
    )


@router.get("/remux/{item_id}")
async def stream_remux(item_id: int, t: float = 0.0) -> StreamingResponse:
    """
    Progressive fragmented-MP4 remux for files whose video is already playable.

    Only the audio is re-encoded and the container is rebuilt, so this costs a
    fraction of a full transcode. `t` seeks, since a fragmented stream cannot be
    range-requested.
    """

    media = playback_url.resolve(item_id)
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
async def get_hls_playlist(item_id: int):
    """
    A static VOD playlist derived from the file's real duration.

    Segments are a fixed length because the encoder is told to force keyframes
    at exactly those boundaries, so what this advertises is what the session
    actually produces.
    """

    media = playback_url.resolve(item_id)
    result = transcode.probe(media.url, cache_key=media.filename)

    return Response(
        content=transcode.build_playlist(result.duration),
        media_type="application/vnd.apple.mpegurl",
        headers={"cache-control": "no-store"},
    )


@router.get("/hls/{item_id}/segment/{seq}.ts")
async def get_hls_segment(item_id: int, seq: int) -> Response:
    """
    Serve one segment from the item's running transcode session.

    The session is persistent: the old implementation started a new ffmpeg for
    every segment, each one re-opening the remote debrid URL and seeking from
    the beginning of the file.
    """

    if seq < 0:
        raise HTTPException(status_code=400, detail="Invalid segment")

    media = playback_url.resolve(item_id)
    result = transcode.probe(media.url, cache_key=media.filename)

    data = await _session_manager.segment(
        item_id=item_id,
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
async def stop_hls_session(item_id: int) -> dict[str, bool]:
    """Tear down an item's session when the player closes."""

    await _session_manager.stop(item_id)

    return {"success": True}
