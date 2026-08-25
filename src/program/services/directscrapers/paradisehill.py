"""paradisehill.cc -- full films, not tube clips, split into per-scene parts.

The other scrapers in this package each resolve one video to one thing worth
watching in various qualities. This site does not have that shape: a title
here is a full-length film, embedded on its page as several ``sources``
entries -- one video per part of the film, at one quality each, not several
qualities of the same part. Modelling that as ``DirectSource`` renditions
would be a lie (playing "index 1" would not play a lower-quality version of
the same thing, it would play a different scene), so the parts are exposed in
film order instead of quality order, labelled by their position.

The site also sits behind an age gate on ``/``, but that only matters if
something requests the root page. Every path this scraper actually uses --
``/search/`` and a title's own page -- answers directly with no cookie or
confirmation step, which was checked rather than assumed.
"""

import json
import re
from urllib.parse import urljoin

from loguru import logger
from lxml import html as lxml_html

from program.services.directscrapers.base import DirectScraper, DirectSource, DirectVideo


#: The `en.` host answers in English; the bare domain redirects there anyway,
#: but skipping the redirect saves a request on every call.
_VIDEO_LIST_RE = re.compile(r"var\s+videoList\s*=\s*(\[.*?\]);", re.S)


class ParadiseHillScraper(DirectScraper):
    key = "paradisehill"
    name = "ParadiseHill"
    base_url = "https://en.paradisehill.cc"

    def search(self, query: str, limit: int = 20) -> list[DirectVideo]:
        response = self._get(
            f"{self.base_url}/search/",
            # what=1 scopes results to films; the search box also offers
            # actors and studios, neither of which is a video to play.
            params={"pattern": query, "what": 1},
        )
        tree = lxml_html.fromstring(response.text)

        videos: list[DirectVideo] = []
        for item in tree.xpath(
            "//div[contains(concat(' ', normalize-space(@class), ' '), ' list-film-item ')]"
        ):
            link = item.xpath("./a[@href]")
            if not link:
                continue
            link = link[0]
            href = link.get("href") or ""
            video_id = href.strip("/")
            if not video_id or video_id in {"categories", "login", "news", "porn"}:
                continue

            title = link.xpath(".//span[@itemprop='name']")
            image = link.xpath(".//img[@itemprop='image']")
            thumbnail = image[0].get("src") if image else None

            videos.append(
                DirectVideo(
                    site=self.key,
                    video_id=video_id,
                    title=(title[0].text_content().strip() if title else "Untitled"),
                    page_url=urljoin(self.base_url, href),
                    thumbnail=urljoin(self.base_url, thumbnail) if thumbnail else None,
                    # Not on the search card -- only the title page states it,
                    # and that is one request per result rather than per click.
                    duration=None,
                )
            )
            if len(videos) >= limit:
                break

        return videos

    def resolve(self, video_id: str) -> list[DirectSource]:
        response = self._get(f"{self.base_url}/{video_id}/")
        match = _VIDEO_LIST_RE.search(response.text)
        if not match:
            logger.debug(f"paradisehill: no videoList on {video_id}")
            return []

        try:
            parts = json.loads(match.group(1))
        except json.JSONDecodeError:
            logger.debug(f"paradisehill: videoList on {video_id} did not parse")
            return []

        sources: list[DirectSource] = []
        for index, part in enumerate(parts, start=1):
            for rendition in part.get("sources") or []:
                url = rendition.get("src") or ""
                if not url:
                    continue
                sources.append(
                    DirectSource(
                        url=url,
                        label=f"Part {index}" if len(parts) > 1 else "Full film",
                        mime_type=rendition.get("type") or "video/mp4",
                        headers={"Referer": f"{self.base_url}/{video_id}/"},
                    )
                )
                # One rendition per part is all the page ever lists; a second
                # would be a genuine quality alternative, which this site does
                # not offer, so there is nothing to prefer within a part.
                break

        if not sources:
            logger.debug(f"paradisehill: videoList on {video_id} had no sources")
        return sources
