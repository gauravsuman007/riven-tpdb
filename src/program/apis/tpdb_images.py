"""Performer pictures, downscaled once and kept in the TPDB cache.

TPDB's performer posters are 50-150 KB JPEGs up to ~1300x2000, drawn on a
title page as ~96px-wide cards. Each is fetched once, shrunk to 200px wide
(aspect kept, never enlarged) as WebP -- 5-8 KB, measured -- and stored under
the TPDB cache directory's ``images/``. They share that cache's size budget
and its least-recently-used eviction (``TpdbApi._enforce_size_limit``), so
one setting bounds both.

The resize is ffmpeg, which the image already ships for the stream remuxer:
a plain downscale needs nothing more, and an imaging library would be a new
native dependency for one job.

The URL arrives from a request, so only TPDB's own image hosts are fetched,
over https, without following redirects -- otherwise this would be a way to
make the server request anything on the home network.
"""

from __future__ import annotations

import hashlib
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import requests
from loguru import logger

if TYPE_CHECKING:
    from program.apis.tpdb_api import TpdbApi

WIDTH = 200
#: Wide art (TMDB backdrops, scene stills): the player's loading screen, the
#: home hero and the title page banner. 1280 is sharp full-width on a laptop
#: and ~60-120 KB as WebP, against 300 KB+ for TMDB's original JPEG.
WIDE_WIDTH = 1280
QUALITY = 78
MAX_SOURCE_BYTES = 8 * 1024 * 1024
FAILURE_TTL_SECONDS = 6 * 60 * 60
# `cdn.` serves the full posters; `thumb.` is TPDB's resizing proxy, which
# the `face` field points at.
ALLOWED_HOSTS = frozenset(
    {"cdn.theporndb.net", "thumb.theporndb.net", "image.tmdb.org"}
)
SUBDIR = "images"

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def is_allowed(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    return parsed.scheme == "https" and parsed.hostname in ALLOWED_HOSTS


def _key(url: str, width: int = WIDTH) -> str:
    # The performer size keeps its original key, so pictures cached before
    # wide art existed stay valid.
    material = url if width == WIDTH else f"{width}:{url}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def downscale(source: bytes, width: int = WIDTH) -> bytes:
    """``source`` as WebP no wider than ``width``; raises on failure."""

    result = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "image2pipe", "-i", "pipe:0",
            # min(): a small source is re-encoded, never blown up.
            "-vf", f"scale=w='min(iw,{width})':h=-2:flags=lanczos",
            "-frames:v", "1",
            "-c:v", "libwebp", "-quality", str(QUALITY), "-compression_level", "6",
            "-f", "webp", "pipe:1",
        ],
        input=source,
        capture_output=True,
        timeout=20,
        check=False,
    )

    if result.returncode != 0 or not result.stdout:
        detail = result.stderr.decode("utf-8", "replace").strip()[-200:]
        raise RuntimeError(f"ffmpeg exited {result.returncode}: {detail}")

    return result.stdout


def _fetch(url: str) -> bytes:
    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (riven)"},
        timeout=15,
        allow_redirects=False,
        stream=True,
    )

    try:
        kind = response.headers.get("content-type", "")

        if response.status_code != 200 or not kind.startswith("image/"):
            raise RuntimeError(f"{response.status_code} {kind or 'no type'}")

        body = bytearray()

        for chunk in response.iter_content(64 * 1024):
            body.extend(chunk)

            if len(body) > MAX_SOURCE_BYTES:
                raise RuntimeError("source too large")

        if not body:
            raise RuntimeError("empty body")

        return bytes(body)
    finally:
        response.close()


def performer_image(api: TpdbApi, url: str) -> bytes | None:
    """A performer's picture, 200px wide."""

    return cached_image(api, url, WIDTH)


def wide_image(api: TpdbApi, url: str) -> bytes | None:
    """A backdrop or scene still, 1280px wide."""

    return cached_image(api, url, WIDE_WIDTH)


def cached_image(api: TpdbApi, url: str, width: int) -> bytes | None:
    """The downscaled picture for ``url``: cached, or fetched and stored.

    None when the URL is not TPDB's or the picture cannot be had; a failure
    is remembered for a few hours so a missing picture is not re-fetched on
    every page view.
    """

    if not is_allowed(url):
        return None

    directory: Path | None = api._cache_dir / SUBDIR if api._cache_dir else None
    key = _key(url, width)

    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())

    # A cast row asks for several pictures at once, and two viewers may ask
    # for the same one; the second waits for the first's file.
    with lock:
        try:
            if directory is not None:
                path = directory / f"{key}.webp"
                miss = directory / f"{key}.miss"

                if path.exists():
                    path.touch()  # recently used, for eviction
                    return path.read_bytes()

                try:
                    if time.time() - miss.stat().st_mtime < FAILURE_TTL_SECONDS:
                        return None
                except OSError:
                    pass

            try:
                image = downscale(_fetch(url), width)
            except Exception as exc:  # noqa: BLE001 -- any failure is a miss
                logger.debug(f"Performer picture {url} unavailable: {exc}")

                if directory is not None:
                    directory.mkdir(parents=True, exist_ok=True)
                    (directory / f"{key}.miss").touch()

                return None

            if directory is not None:
                directory.mkdir(parents=True, exist_ok=True)
                # Written whole, then renamed: a reader must never get half.
                partial = directory / f"{key}.tmp"
                partial.write_bytes(image)
                partial.replace(directory / f"{key}.webp")
                api._enforce_size_limit()

            return image
        finally:
            with _locks_guard:
                _locks.pop(key, None)
