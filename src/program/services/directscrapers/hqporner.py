"""hqporner.com -- an index of embeds, not a host of its own.

Search and the video page are both plain server-rendered HTML, but the video
itself never lives on hqporner: the page carries an ``<iframe>`` pointing at
a rotating embed domain (``mydaddy.cc`` at the time of writing, but the whole
point of indexing rather than hosting is that this changes). That embed page
is what actually names the renditions, in a plain unsigned ``<video>`` block
built by inline JavaScript rather than server-rendered markup -- so it is read
with a regex over the script rather than an HTML parser.
"""

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


_IFRAME_RE = re.compile(r'<iframe[^>]+src="([^"]+)"')
#: Matches one <source> the embed page's inline script builds, e.g.
#: `<source src="//cdn/x/1080.mp4" title="1080p60" type="video/mp4" />`.
_SOURCE_RE = re.compile(
    r'<source src=\\?"([^"\\]+)\\?" title=\\?"(\d+)p[^"\\]*\\?"'
)


class HQPornerScraper(DirectScraper):
    key = "hqporner"
    name = "HQPorner"
    base_url = "https://hqporner.com"

    def search(self, query: str, limit: int = 20) -> list[DirectVideo]:
        response = self._get(self.base_url + "/", params={"q": query})
        tree = lxml_html.fromstring(response.text)

        videos: list[DirectVideo] = []
        for card in tree.xpath(
            "//section[contains(concat(' ', normalize-space(@class), ' '), ' feature ')]"
        ):
            link = card.xpath(".//h3[contains(@class, 'meta-data-title')]/a")
            if not link:
                continue
            link = link[0]
            href = link.get("href") or ""
            match = re.search(r"/hdporn/(\d+)", href)
            if not match:
                continue

            image = card.xpath(".//img[@id]")
            thumbnail = image[0].get("src") if image else None
            if thumbnail and thumbnail.startswith("//"):
                thumbnail = "https:" + thumbnail

            duration_text = card.xpath(
                ".//*[contains(@class, 'fa-clock-o')]"
            )

            videos.append(
                DirectVideo(
                    site=self.key,
                    video_id=match.group(1),
                    title=link.text_content().strip() or "Untitled",
                    page_url=urljoin(self.base_url, href),
                    thumbnail=thumbnail,
                    duration=parse_duration(
                        duration_text[0].text_content() if duration_text else None
                    ),
                )
            )
            if len(videos) >= limit:
                break

        return videos

    def resolve(self, video_id: str) -> list[DirectSource]:
        # The bare id 302s to the full slug URL, same as the search result
        # links do -- there is no need to carry the slug alongside it.
        page = self._get(f"{self.base_url}/hdporn/{video_id}.html").text

        iframe = _IFRAME_RE.search(page)
        if not iframe:
            logger.debug(f"hqporner: no embed iframe on video {video_id}")
            return []
        embed_url = iframe.group(1)
        if embed_url.startswith("//"):
            embed_url = "https:" + embed_url
        elif embed_url.startswith("/"):
            embed_url = urljoin(self.base_url, embed_url)

        embed_page = self._get(
            embed_url, headers={"Referer": f"{self.base_url}/"}
        ).text

        # The embed page renders two variants of the same <video> block -- a
        # bare 360p one for adblock users, the full ladder for everyone else
        # -- as two separate JS strings the regex has no reason to tell apart.
        seen: set[str] = set()
        sources: list[DirectSource] = []
        for url, height in _SOURCE_RE.findall(embed_page):
            if url.startswith("//"):
                url = "https:" + url
            if url in seen:
                continue
            seen.add(url)
            sources.append(
                DirectSource(
                    url=url,
                    label=f"{height}p",
                    resolution=f"{height}p",
                    headers={"Referer": embed_url},
                )
            )

        sources.sort(key=lambda s: int(s.resolution[:-1]) if s.resolution else 0, reverse=True)
        if not sources:
            logger.debug(f"hqporner: no <source> tags in the embed for {video_id}")
        return sources
