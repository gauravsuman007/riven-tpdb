"""Direct streaming-site search.

Separate from ``program.services.scrapers`` on purpose. Those find torrents,
which go through ranking, a debrid provider and the VFS before anything can be
played. These find a file on a website and play it -- no infohash, no download,
nothing added to the library. Sharing a base class would have forced one of the
two into the other's shape.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

from loguru import logger

from program.services.directscrapers.base import DirectScraper
from program.services.directscrapers.iporntv import IPornTVScraper
from program.services.directscrapers.models import DirectSource, DirectVideo
from program.services.directscrapers.upornia import UporniaScraper
from program.services.directscrapers.xfreehd import XFreeHDScraper


class DirectScraperService:
    """Runs every direct scraper against one query and merges the results."""

    key = "direct_scraping"

    def __init__(self) -> None:
        self.services: dict[str, DirectScraper] = {
            scraper.key: scraper
            for scraper in (
                XFreeHDScraper(),
                UporniaScraper(),
                IPornTVScraper(),
            )
        }
        self.initialized = True

    def search(
        self,
        query: str,
        limit_per_site: int = 20,
        sites: list[str] | None = None,
    ) -> tuple[list[DirectVideo], dict[str, str]]:
        """Search every site at once.

        Returns the results and a map of site key to error message. One site
        being down is normal -- these are not services with uptime guarantees --
        and must not cost the user the other two, so failures are reported
        alongside the results rather than raised.
        """

        selected = {
            key: scraper
            for key, scraper in self.services.items()
            if not sites or key in sites
        }

        results: dict[str, list[DirectVideo]] = {}
        errors: dict[str, str] = {}

        with ThreadPoolExecutor(max_workers=len(selected) or 1) as executor:
            futures = {
                executor.submit(scraper.search, query, limit_per_site): key
                for key, scraper in selected.items()
            }
            for future in as_completed(futures):
                key = futures[future]
                try:
                    results[key] = future.result()
                except Exception as exc:
                    logger.warning(f"Direct scraper {key} failed: {exc}")
                    errors[key] = str(exc)
                    results[key] = []

        return _interleave(selected, results), errors

    def resolve(self, site: str, video_id: str) -> list[DirectSource]:
        """Resolve one video to playable URLs.

        Always live. Several of these URLs are signed with an expiry, and one
        site binds the URL to the IP that asked for it, so a cached value is a
        403 waiting to happen.
        """

        scraper = self.services.get(site)
        if scraper is None:
            raise ValueError(f"Unknown direct scraper: {site}")
        return scraper.resolve(video_id)


def _interleave(
    selected: dict[str, DirectScraper], results: dict[str, list[DirectVideo]]
) -> list[DirectVideo]:
    """Round-robin the per-site lists into one.

    Concatenating instead would bury the smaller sites: one site returns 60
    results and the others 30, so the first two screens would be a single
    source. Taking one from each in turn keeps every site visible from the top
    while preserving each site's own relevance order.
    """

    merged: list[DirectVideo] = []
    seen: set[str] = set()
    order = list(selected)

    for index in range(max((len(v) for v in results.values()), default=0)):
        for key in order:
            bucket = results.get(key) or []
            if index >= len(bucket):
                continue
            video = bucket[index]
            if video.key() in seen:
                continue
            seen.add(video.key())
            merged.append(video)

    return merged


__all__ = ["DirectScraperService", "DirectSource", "DirectVideo"]
