"""tnaflix.com -- server-rendered, and the same shape as xfreehd.

Search results and the video page are both plain HTML with no token dance:
the video page's ``<video>`` element lists one unsigned-looking ``<source>``
per rendition, each carrying a ``size`` attribute that is the resolution in
pixel height (``144``, ``240``, ... ``1080``) rather than a label, which is
why it is turned into a ``"720p"``-style string before anything else sees it.

The one trap is the thumbnail, same as xfreehd: the first few cards on a
result page render ``fetchpriority="high"`` with a real ``src``, but the rest
lazy-load through ``data-src`` and carry a shared placeholder in ``src``.
"""

import re
from urllib.parse import urljoin

from loguru import logger
from lxml import html as lxml_html

from program.services.directscrapers.base import (
    DirectScraper,
    DirectSource,
    DirectVideo,
    parse_count,
    parse_duration,
)


class TnaflixScraper(DirectScraper):
    key = "tnaflix"
    name = "TNAFlix"
    base_url = "https://www.tnaflix.com"

    def search(self, query: str, limit: int = 20) -> list[DirectVideo]:
        response = self._get(f"{self.base_url}/search", params={"what": query})
        tree = lxml_html.fromstring(response.text)

        videos: list[DirectVideo] = []
        for card in tree.xpath("//div[@data-vid]"):
            video_id = card.get("data-vid") or ""
            if not video_id:
                continue

            title_link = card.xpath(".//a[contains(@class, 'video-title')]")
            if not title_link:
                continue
            title_link = title_link[0]
            href = title_link.get("href") or ""

            image = card.xpath(".//img")
            thumbnail = ""
            if image:
                # data-src first: only the first handful of cards on a page
                # carry a real `src`, the rest a shared lazy-load placeholder.
                thumbnail = image[0].get("data-src") or image[0].get("src") or ""
                if "placeholder" in thumbnail:
                    thumbnail = ""

            resolution = _text(card, "max-quality") or None

            videos.append(
                DirectVideo(
                    site=self.key,
                    video_id=video_id,
                    title=title_link.text_content().strip() or "Untitled",
                    page_url=urljoin(self.base_url, href),
                    thumbnail=thumbnail or None,
                    duration=parse_duration(_text(card, "video-duration")),
                    resolution=resolution,
                    hd=resolution in {"720p", "1080p", "1440p", "2160p"},
                    views=parse_count(_views_text(card)),
                )
            )
            if len(videos) >= limit:
                break

        return videos

    def resolve(self, video_id: str) -> list[DirectSource]:
        # The search result's own page_url is not carried through to /resolve,
        # but this path 302s from the numeric id alone, same as the video-id
        # card links do.
        response = self._get(f"{self.base_url}/-/video{video_id}")
        tree = lxml_html.fromstring(response.text)

        sources: list[DirectSource] = []
        for tag in tree.xpath("//video//source"):
            url = tag.get("src") or ""
            if not url.startswith("http"):
                continue
            height = tag.get("size") or ""
            label = f"{height}p" if height.isdigit() else "Source"
            sources.append(
                DirectSource(
                    url=url,
                    label=label,
                    resolution=label if label != "Source" else None,
                    headers={"Referer": self.base_url + "/"},
                )
            )

        sources.sort(
            key=lambda s: int(s.resolution[:-1]) if s.resolution else 0,
            reverse=True,
        )
        if not sources:
            logger.debug(f"tnaflix: no <source> tags on video {video_id}")
        return sources


def _text(element, class_name: str) -> str:
    """Text of the first descendant carrying `class_name`."""

    found = element.xpath(
        f".//*[contains(concat(' ', normalize-space(@class), ' '), ' {class_name} ')]"
    )
    return found[0].text_content().strip() if found else ""


def _views_text(card) -> str:
    """The view count, e.g. ``3.7K``.

    Lives on the parent of the ``icon-eye`` glyph rather than on it: the icon
    element itself carries no text, only the number sitting next to it does.
    """

    icon = card.xpath(
        ".//i[contains(concat(' ', normalize-space(@class), ' '), ' icon-eye ')]"
    )
    return icon[0].getparent().text_content().strip() if icon else ""
