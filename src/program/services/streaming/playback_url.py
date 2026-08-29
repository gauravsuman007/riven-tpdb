"""
Playable-URL resolution for the HTTP streaming endpoints.

RivenVFS refreshes debrid links as it reads (`MediaStream._refresh_download_url`),
but the HTTP endpoints in `routers.secure.stream` historically used
`MediaEntry.url` verbatim. That value is stored, never refreshed, and for
providers whose links are minted per request it is either

  * an expired CDN link -- TorBox answers those with 400, not 404, so even
    `DebridCDNUrl.validate()` would not have refreshed it, or
  * an internal reference such as `torbox://{download_id}/{file_id}`, which no
    HTTP client and no ffmpeg build can open at all.

Both cases surfaced as a dead player: 502 on direct play, "Transcoding failed"
on the HLS path.

This module mints a fresh URL through the provider, exactly as the VFS does, but
*without* `VFSDatabase.refresh_unrestricted_url`'s failure behaviour -- that
helper resets and blacklists the media item when unrestricting fails, which
would silently un-complete a downloaded title just because a playback attempt
raced a provider hiccup. Playback is a read; it must never mutate the library.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from kink import di
from loguru import logger

from program.db.db import db_session
from program.media.media_entry import MediaEntry

# Anything the stored URL might be that an HTTP client cannot open. Providers
# use these to mean "ask me for a real link"; they are not transport schemes.
_INTERNAL_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
_PLAYABLE_SCHEME = re.compile(r"^https?://", re.I)

# Keys are secrets even though they travel inside a URL, so log lines redact
# them rather than relying on nobody reading the logs.
_TOKEN_PARAM = re.compile(r"([?&](?:token|apikey|api_key|auth)=)[^&]*", re.I)


def redact(url: str | None) -> str:
    """Make a provider URL safe to log."""

    if not url:
        return "<none>"

    return _TOKEN_PARAM.sub(r"\1REDACTED", url)


@dataclass(frozen=True)
class PlayableMedia:
    """A resolved, HTTP-fetchable target for one media item."""

    url: str
    provider: str
    filename: str
    #: Bytes, as recorded when the file was downloaded. 0 when unknown --
    #: never guessed from the URL, which would be a claim the data cannot
    #: support.
    file_size: int = 0


def is_playable(url: str | None) -> bool:
    """True when `url` is something an HTTP client or ffmpeg can actually open."""

    return bool(url and _PLAYABLE_SCHEME.match(url))


def _provider_service(provider: str | None):
    """Find the downloader service that owns `provider`, if it is loaded."""

    if not provider:
        return None

    try:
        from program.services.filesystem.vfs.db import VFSDatabase

        downloader = di[VFSDatabase].downloader
    except Exception:
        # The VFS is not up (or DI is not wired, as in tests). Nothing to
        # resolve with -- the caller falls back to the stored URL.
        return None

    if not downloader:
        return None

    return next(
        (svc for svc in downloader.services.values() if svc.key == provider),
        None,
    )


def mint_url(original_filename: str) -> str | None:
    """
    Ask the provider for a fresh URL for `original_filename` and persist it.

    Returns None when the entry is unknown, the provider is not loaded, or the
    provider declines. Never raises for an ordinary provider failure, and never
    mutates the MediaItem -- see the module docstring.
    """

    with db_session() as session:
        entry = (
            session.query(MediaEntry)
            .filter(MediaEntry.original_filename == original_filename)
            .first()
        )

        if not entry or not entry.download_url:
            return None

        service = _provider_service(entry.provider)

        if not service:
            logger.debug(
                f"No loaded downloader for provider {entry.provider!r}; "
                f"cannot mint a URL for {original_filename}"
            )
            return None

        try:
            minted = service.unrestrict_link(entry.download_url)
        except Exception as e:
            logger.warning(f"Could not mint a playback URL for {original_filename}: {e}")
            return None

        if not minted or not is_playable(minted.download):
            return None

        entry.unrestricted_url = minted.download
        session.commit()

        logger.debug(
            f"Minted playback URL for {original_filename}: {redact(minted.download)}"
        )

        return minted.download


def verify(url: str) -> bool:
    """
    Cheaply check that `url` still serves data.

    A stored CDN link can be well-formed and still be spent -- TorBox answers
    those with 400. Callers that hand the URL to ffmpeg need to know before they
    do, because ffmpeg cannot come back and ask for a fresh one.
    """

    import httpx

    try:
        with httpx.Client(follow_redirects=True, timeout=15) as client:
            # One byte is enough to learn whether the link is alive, and avoids
            # pulling a gigabyte to find out.
            response = client.get(url, headers={"Range": "bytes=0-0"})

            return response.status_code < 400
    except Exception as e:
        logger.debug(f"Could not verify {redact(url)}: {redact(str(e))}")

        return False


def resolve(
    item_id: int, *, force: bool = False, check: bool = False
) -> PlayableMedia:
    """
    Resolve a media item to a URL that can be fetched right now.

    Args:
        item_id: MediaItem id.
        force: Mint a new URL even when the stored one looks usable. Callers
            pass this after the stored URL has been rejected upstream.
        check: Verify the stored URL before returning it, minting a new one if
            it is dead. Callers that pass the URL to ffmpeg want this: ffmpeg
            gets one attempt and cannot retry through this module.

    Raises:
        HTTPException: 404 when the item has no media, 502 when no playable URL
            can be produced.
    """

    from fastapi import HTTPException

    from program.media.item import MediaItem

    with db_session() as session:
        item = session.get(MediaItem, item_id)

        if not item:
            raise HTTPException(status_code=404, detail="Item not found")

        if not item.media_entry:
            raise HTTPException(status_code=404, detail="Item has no media file")

        stored = item.media_entry.url
        provider = item.media_entry.provider or ""
        filename = item.media_entry.original_filename
        file_size = item.media_entry.file_size or 0

    # A stored URL is only worth trying when it is a real HTTP URL. An internal
    # reference has to be minted regardless of `force`.
    if not force and is_playable(stored) and (not check or verify(stored)):
        return PlayableMedia(url=stored, provider=provider, filename=filename, file_size=file_size)

    if minted := mint_url(filename):
        return PlayableMedia(url=minted, provider=provider, filename=filename, file_size=file_size)

    # Minting produced nothing usable. A stored URL that just failed `check` is
    # no better, so only fall back to one that was never tested.

    if is_playable(stored):
        # Minting failed but the stored URL is at least well-formed; let the
        # caller try it and surface the provider's own error.
        return PlayableMedia(url=stored, provider=provider, filename=filename, file_size=file_size)

    scheme_note = (
        f" (stored value {stored.split('://')[0]}:// is not fetchable)"
        if stored and _INTERNAL_SCHEME.match(stored)
        else ""
    )

    raise HTTPException(
        status_code=502,
        detail=f"Could not obtain a playable URL from {provider or 'the provider'}{scheme_note}",
    )
