"""upornia.com -- a Vue SPA with an undocumented but perfectly usable JSON API.

The obvious route here is Playwright, because the search page renders nothing
without JavaScript. That would put a headless Chromium (~110 MB, plus its
system libraries) into the backend image to read a list of video titles. It is
not necessary: the SPA is talking to a plain JSON API, and this talks to the
same one. Search is a single request and returns the file's real dimensions and
byte size, which the other two sites only reveal on the video page.

The media URL is obfuscated rather than encrypted. It arrives base64-encoded
with a handful of characters swapped for Cyrillic homoglyphs -- visually
identical, so the string looks like ordinary base64 and fails to decode. Undo
the substitution and it is a normal URL. It is also time-limited and served
from a signed CDN, so it must be resolved at play time, never cached.
"""

import base64
from urllib.parse import urljoin

from loguru import logger

from program.services.directscrapers.base import (
    DirectScraper,
    DirectSource,
    DirectVideo,
    parse_count,
    parse_duration,
    resolution_from_dimensions,
)


#: Cyrillic letters that render identically to Latin ones, used to corrupt the
#: base64 payload. Mapping them back is the whole of the "decryption".
_HOMOGLYPHS = str.maketrans(
    "АВЕКМНОРСТХ"
    "аеорсух"
    #: Not a homoglyph: a comma stands in for the "/" before the query string.
    #: Only some videos use it, which is why a decoder that handles the
    #: lookalikes alone works on most of the catalogue and silently fails on
    #: the rest.
    ",",
    "ABEKMHOPCTXaeopcyx"
    "/",
)


class UporniaScraper(DirectScraper):
    key = "upornia"
    name = "Upornia"
    base_url = "https://upornia.com"

    def search(self, query: str, limit: int = 20) -> list[DirectVideo]:
        # params is a positional path baked into a query string by the SPA:
        #   {lifetime}/{gender}/{sort}/{count}/search.{object}.{page}.{type}.
        #   {duration}.{date}
        payload = self._get(
            f"{self.base_url}/api/videos2.php",
            params={
                "params": f"86400/str/relevance/{max(limit, 20)}/search..1.all..",
                "s": query,
            },
            headers={"Accept": "application/json", "Referer": f"{self.base_url}/"},
        ).json()

        videos: list[DirectVideo] = []
        for entry in payload.get("videos", []):
            video_id = str(entry.get("video_id") or "")
            if not video_id:
                continue

            videos.append(
                DirectVideo(
                    site=self.key,
                    video_id=video_id,
                    title=(entry.get("title") or "Untitled").strip(),
                    page_url=urljoin(
                        self.base_url, f"/videos/{video_id}/{entry.get('dir', '')}/"
                    ),
                    thumbnail=entry.get("scr") or None,
                    duration=parse_duration(entry.get("duration")),
                    resolution=resolution_from_dimensions(
                        entry.get("file_dimensions")
                    ),
                    size=_best_size(entry.get("file_formats")),
                    views=parse_count(entry.get("video_viewed")),
                    hd=str((entry.get("props") or {}).get("hd", "")) == "1",
                )
            )
            if len(videos) >= limit:
                break

        return videos

    def resolve(self, video_id: str) -> list[DirectSource]:
        formats = self._get(
            f"{self.base_url}/api/videofile.php",
            params={"video_id": video_id, "lifetime": 864000},
            headers={"Accept": "application/json", "Referer": f"{self.base_url}/"},
        ).json()

        sources: list[DirectSource] = []
        for entry in formats or []:
            path = _deobfuscate(entry.get("video_url") or "")
            if not path:
                continue
            sources.append(
                DirectSource(
                    url=urljoin(self.base_url, path),
                    label=(entry.get("format") or "").lstrip(".").upper() or "Source",
                    # Referer is not optional here: the get_file handler
                    # redirects to a signed CDN URL and refuses without it.
                    headers={"Referer": f"{self.base_url}/"},
                )
            )

        if not sources:
            logger.debug(f"upornia: no playable format for video {video_id}")
        # is_default first, then whatever order the API gave.
        sources.sort(key=lambda s: s.label != "MP4")
        return sources


def _deobfuscate(value: str) -> str:
    """Undo the homoglyph substitution and decode the base64 path."""

    if not value:
        return ""
    normalised = value.translate(_HOMOGLYPHS).replace("~", "=")
    # Substituting a two-character sequence for one comma leaves the payload a
    # byte or two short of a multiple of four.
    normalised += "=" * (-len(normalised) % 4)
    try:
        decoded = base64.b64decode(normalised).decode("utf-8", "replace")
    except Exception:
        logger.debug("upornia: video_url did not decode as base64")
        return ""

    if not decoded.startswith("/"):
        # A partial decode yields mojibake rather than an exception, and a
        # mojibake "URL" fails later as an opaque 404.
        logger.debug("upornia: decoded video_url is not a path")
        return ""
    return decoded


def _best_size(file_formats: str | None) -> int | None:
    """Pull the largest byte count out of the packed ``file_formats`` string.

    The field is pipe-delimited groups of
    ``|<suffix>|<dimensions>|<bitrate>|<bytes>|...`` with one group per
    rendition, including the short preview clip. The largest figure is the full
    video; the rest are trailers.
    """

    if not file_formats:
        return None
    sizes = [
        int(part)
        for part in file_formats.split("|")
        if part.isdigit() and int(part) > 1_000_000
    ]
    return max(sizes) if sizes else None
