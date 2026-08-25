"""eporner.com -- the only one of the five with a documented, public JSON API.

Search is a single request to ``/api/v2/video/search/`` and needs no HTML
parsing at all. The trade-off is that the API is search-only: it hands back
title, thumbnail and duration but nothing about renditions, so quality is
still only knowable once a video page is fetched to resolve it.

The video page lists direct MP4 download links per resolution rather than
``<source>`` tags. They are not signed at the link itself -- the signing
happens on the redirect this URL 302s through -- so httpx's default
redirect-following in the streaming proxy resolves them the same as any other
site's source.
"""

import re
from urllib.parse import urljoin

from loguru import logger
from lxml import html as lxml_html

from program.services.directscrapers.base import DirectScraper, DirectSource, DirectVideo


_DOWNLOAD_RE = re.compile(
    r"Download MP4 \((\d+)p, [^,]+, ([\d.]+)\s*(MB|GB)\)"
)


class EPornerScraper(DirectScraper):
    key = "eporner"
    name = "EPorner"
    base_url = "https://www.eporner.com"

    def search(self, query: str, limit: int = 20) -> list[DirectVideo]:
        payload = self._get(
            f"{self.base_url}/api/v2/video/search/",
            params={
                "query": query,
                "per_page": max(limit, 20),
                "thumbsize": "medium",
                "order": "most-relevant",
                "format": "json",
            },
        ).json()

        videos: list[DirectVideo] = []
        for entry in payload.get("videos", []):
            video_id = entry.get("id") or ""
            if not video_id:
                continue

            thumb = entry.get("default_thumb") or {}
            videos.append(
                DirectVideo(
                    site=self.key,
                    video_id=video_id,
                    title=(entry.get("title") or "Untitled").strip(),
                    page_url=entry.get("url") or urljoin(self.base_url, f"/video-{video_id}/-/"),
                    thumbnail=thumb.get("src"),
                    duration=entry.get("length_sec"),
                    # The search API does not report a rendition, only the
                    # video page does -- filled in once the user resolves it.
                    resolution=None,
                    views=entry.get("views"),
                )
            )
            if len(videos) >= limit:
                break

        return videos

    def resolve(self, video_id: str) -> list[DirectSource]:
        # The slug in a search result's page_url is cosmetic: "-" 302s to the
        # canonical URL the same as the real one, so it does not need to be
        # carried alongside the id.
        response = self._get(f"{self.base_url}/video-{video_id}/-/")
        tree = lxml_html.fromstring(response.text)

        sources: list[DirectSource] = []
        for link in tree.xpath("//a[contains(@href, '/dload/')]"):
            href = link.get("href") or ""
            if not href:
                continue
            match = _DOWNLOAD_RE.search(link.text_content())
            label = f"{match.group(1)}p" if match else "Source"
            size = None
            if match:
                value = float(match.group(2))
                size = int(value * (1_000_000_000 if match.group(3) == "GB" else 1_000_000))
            sources.append(
                DirectSource(
                    url=urljoin(self.base_url, href),
                    label=label,
                    resolution=label if label != "Source" else None,
                    size=size,
                    headers={"Referer": f"{self.base_url}/video-{video_id}/-/"},
                )
            )

        sources.sort(
            key=lambda s: int(s.resolution[:-1]) if s.resolution else 0,
            reverse=True,
        )
        if not sources:
            logger.debug(f"eporner: no download links on video {video_id}")
        return sources
