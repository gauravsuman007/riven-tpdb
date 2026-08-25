"""tubepornclassic.com -- the same platform as upornia, different catalogue.

Confirmed rather than assumed: its search and file-resolution endpoints are
byte-for-byte the same shape as upornia's (``/api/videos2.php``,
``/api/videofile.php``), and a captured ``video_url`` decodes correctly
through upornia's homoglyph-substitution logic unchanged. Sharing that
decoder is not a guess dressed up as reuse -- it was checked against a live
response before this scraper was written to lean on it.
"""

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
from program.services.directscrapers.upornia import _best_size, _deobfuscate


class TubePornClassicScraper(DirectScraper):
    key = "tubepornclassic"
    name = "TubePornClassic"
    base_url = "https://tubepornclassic.com"

    def search(self, query: str, limit: int = 20) -> list[DirectVideo]:
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
                    views=parse_count(str(entry.get("video_viewed") or "")),
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
                    headers={"Referer": f"{self.base_url}/"},
                )
            )

        if not sources:
            logger.debug(f"tubepornclassic: no playable format for video {video_id}")
        sources.sort(key=lambda s: s.label != "MP4")
        return sources
