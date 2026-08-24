"""iporntv.net -- server-rendered search, JS-assembled media URL.

The player URL is never present in the HTML as a string. It is split across a
few dozen ``var iXXXX<n> = "..."`` fragments and concatenated by a one-line
script, purely to defeat a regex. Rather than run a browser, this reads the
concatenation expression and joins the same fragments in the same order, which
is both exact and far cheaper.

Search returns two different link shapes and only one of them has a player:

    /download/<numeric id>/<slug>   -- carries the fragments itself
    /download/video/<hex id>/<slug> -- player lives in an /embed/ iframe

The hex shape is half of every result page, so treating it as unresolvable
would silently throw away half the catalogue. Both are handled.
"""

import base64
import re
from urllib.parse import urljoin

from loguru import logger
from lxml import html as lxml_html

from program.services.directscrapers.base import (
    DirectScraper,
    DirectSource,
    DirectVideo,
    parse_duration,
)


_VAR_RE = re.compile(r'var\s+([A-Za-z_]\w*)\s*=\s*"([^"]*)"\s*;')
#: The assembly line: `var x = a + b + c + ...;`. Two or more operands, so a
#: stray `var a = b + c` elsewhere on the page cannot be mistaken for it.
_CONCAT_RE = re.compile(
    r'var\s+([A-Za-z_]\w*)\s*=\s*((?:[A-Za-z_]\w*\s*\+\s*){2,}[A-Za-z_]\w*)\s*;'
)
_NUMERIC_ID_RE = re.compile(r"/download/(\d+)/")
_HEX_ID_RE = re.compile(r"/download/video/([a-f0-9]+)/")


class IPornTVScraper(DirectScraper):
    key = "iporntv"
    name = "iPornTV"
    base_url = "https://iporntv.net"

    def search(self, query: str, limit: int = 20) -> list[DirectVideo]:
        response = self._get(
            f"{self.base_url}/searches.php", params={"word": query}
        )
        tree = lxml_html.fromstring(response.text)

        videos: list[DirectVideo] = []
        for item in tree.xpath(
            "//*[contains(concat(' ', normalize-space(@class), ' '), ' item-post ')]"
        ):
            links = item.xpath(
                ".//a[contains(concat(' ', normalize-space(@class), ' '), ' link ')]"
            )
            if not links:
                continue
            link = links[0]
            href = link.get("href") or ""

            video_id = _video_id(href)
            if not video_id:
                # Without an id there is nothing to resolve later, so the card
                # would be a dead tile. Drop it rather than show it.
                continue

            images = item.xpath(
                ".//*[contains(concat(' ', normalize-space(@class), ' '), ' wrap_image ')]//img"
            )
            thumbnail = ""
            if images:
                thumbnail = (
                    images[0].get("data-src") or images[0].get("src") or ""
                )

            duration = item.xpath(
                ".//*[contains(concat(' ', normalize-space(@class), ' '), ' b-time ')]"
                "//*[contains(concat(' ', normalize-space(@class), ' '), ' value ')]"
            )

            videos.append(
                DirectVideo(
                    site=self.key,
                    video_id=video_id,
                    title=(link.get("title") or link.text_content()).strip()
                    or "Untitled",
                    page_url=urljoin(self.base_url, href),
                    thumbnail=thumbnail or None,
                    duration=parse_duration(
                        duration[0].text_content() if duration else None
                    ),
                )
            )
            if len(videos) >= limit:
                break

        return videos

    def resolve(self, video_id: str) -> list[DirectSource]:
        # A hex id addresses the embed player directly; the numeric ids have
        # their fragments on the download page itself.
        if _is_hex_id(video_id):
            token = base64.b64encode(video_id.encode()).decode()
            page = self._get(
                f"{self.base_url}/embed/",
                params={"vod": token, "state": "active"},
                headers={"Referer": f"{self.base_url}/"},
            ).text
        else:
            page = self._get(f"{self.base_url}/download/{video_id}/-").text

        url = _assemble_url(page)
        if not url:
            logger.debug(f"iporntv: could not assemble a media URL for {video_id}")
            return []

        # The same endpoint serves either a progressive MP4 or an HLS
        # playlist depending on the video, and nothing in the URL says which.
        # One HEAD is cheaper than a player that renders a blank frame.
        mime_type = "video/mp4"
        try:
            probe = self.session.head(
                url,
                headers={"Referer": f"{self.base_url}/"},
                timeout=15,
                allow_redirects=True,
            )
            content_type = (probe.headers.get("content-type") or "").split(";")[0]
            if content_type:
                mime_type = content_type
        except Exception as exc:
            logger.debug(f"iporntv: could not probe content type for {video_id}: {exc}")

        return [
            DirectSource(
                url=url,
                label="Source",
                mime_type=mime_type,
                headers={"Referer": f"{self.base_url}/"},
            )
        ]


def _is_hex_id(video_id: str) -> bool:
    return not video_id.isdigit()


def _video_id(href: str) -> str:
    match = _HEX_ID_RE.search(href) or _NUMERIC_ID_RE.search(href)
    return match.group(1) if match else ""


def _assemble_url(page: str) -> str:
    """Rebuild the concatenated player URL from a page's script fragments."""

    fragments = dict(_VAR_RE.findall(page))
    if not fragments:
        return ""

    for match in _CONCAT_RE.finditer(page):
        operands = [part.strip() for part in match.group(2).split("+")]
        # Every operand must be a known string fragment. A partial join would
        # produce a plausible-looking but broken URL, which is worse than none.
        if not all(operand in fragments for operand in operands):
            continue
        candidate = "".join(fragments[operand] for operand in operands)
        if candidate.startswith("http"):
            return candidate

    return ""
