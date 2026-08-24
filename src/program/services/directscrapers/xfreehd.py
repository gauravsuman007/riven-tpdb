"""xfreehd.com -- server-rendered, and the simplest of the three.

Search results are plain HTML and the video page carries unsigned ``<source>``
tags, so nothing here needs a browser or a token dance. The one trap is the
thumbnail: every card's ``<img src>`` is a shared placeholder and the real
image lives in ``data-src``, which is why results used to render as a wall of
identical grey tiles.
"""

import re
from urllib.parse import urljoin

import requests

from loguru import logger
from lxml import html as lxml_html

from program.services.directscrapers.base import (
    DirectScraper,
    DirectSource,
    DirectVideo,
    parse_count,
    parse_duration,
)


#: Source tags are labelled ``hd``/``sd`` rather than by height, so the label
#: is all there is to sort on. Higher is better.
_QUALITY_RANK = {"4k": 4, "uhd": 4, "hd": 3, "sd": 2, "low": 1}


class XFreeHDScraper(DirectScraper):
    key = "xfreehd"
    name = "XFreeHD"
    base_url = "https://beta.xfreehd.com"

    def search(self, query: str, limit: int = 20) -> list[DirectVideo]:
        try:
            response = self._get(
                f"{self.base_url}/search",
                params={"search_query": query, "search_type": "videos"},
            )
        except requests.HTTPError as exc:
            # The site answers "nothing matched" with a 404 carrying a normal
            # page. Raising here would report a working site as broken and, in
            # a multi-site search, make one dud query look like an outage.
            if exc.response is not None and exc.response.status_code == 404:
                return []
            raise
        tree = lxml_html.fromstring(response.text)

        videos: list[DirectVideo] = []
        for link in tree.xpath("//a[contains(@class, 'video-link')]"):
            href = link.get("href") or ""
            match = re.search(r"/video/(\d+)/", href)
            if not match:
                continue

            # Private videos need an account: the page renders a login prompt
            # with no <source>, so the card would resolve to nothing.
            if link.xpath(
                ".//*[contains(concat(' ', normalize-space(@class), ' '),"
                " ' label-private ')]"
            ):
                continue

            title = _text(link, "video-title-new")
            image = link.xpath(".//img")
            # data-src first: `src` is the lazy-load placeholder on every card.
            thumbnail = ""
            if image:
                thumbnail = image[0].get("data-src") or image[0].get("src") or ""
                if thumbnail and not thumbnail.startswith("http"):
                    thumbnail = urljoin(self.base_url, thumbnail)
                if "ximgx" in thumbnail:
                    thumbnail = ""

            videos.append(
                DirectVideo(
                    site=self.key,
                    video_id=match.group(1),
                    title=title or "Untitled",
                    page_url=urljoin(self.base_url, href),
                    thumbnail=thumbnail or None,
                    duration=parse_duration(_text(link, "duration-new")),
                    # The HD badge is a claim about the file, not a
                    # measurement -- the same badge covers 720p and 4K -- so it
                    # is recorded as a hint and never as a resolution.
                    resolution=None,
                    hd=bool(
                        link.xpath(
                            ".//*[contains(concat(' ', normalize-space(@class),"
                            " ' '), ' hd-text-icon ')]"
                        )
                    ),
                    views=parse_count(_text(link, "video-views-new")),
                )
            )
            if len(videos) >= limit:
                break

        return videos

    def resolve(self, video_id: str) -> list[DirectSource]:
        response = self._get(f"{self.base_url}/video/{video_id}/-")
        tree = lxml_html.fromstring(response.text)

        sources: list[DirectSource] = []
        for tag in tree.xpath("//video//source | //source[@type='video/mp4']"):
            url = tag.get("src") or ""
            if not url.startswith("http"):
                continue
            label = (tag.get("title") or "").strip() or "Video"
            sources.append(
                DirectSource(
                    url=url,
                    label=label.upper(),
                    headers={"Referer": self.base_url + "/"},
                )
            )

        sources.sort(
            key=lambda s: _QUALITY_RANK.get(s.label.lower(), 0), reverse=True
        )
        if not sources:
            logger.debug(f"xfreehd: no <source> tags on video {video_id}")
        return sources


def _text(element, class_name: str) -> str:
    """Text of the first descendant carrying `class_name`.

    XPath rather than a CSS selector because lxml's cssselect support is an
    optional dependency that is not installed in the runtime image.
    """

    found = element.xpath(
        f".//*[contains(concat(' ', normalize-space(@class), ' '), ' {class_name} ')]"
    )
    return found[0].text_content().strip() if found else ""
