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
from program.services.directscrapers.ranking import best_matches, sort_key
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

    #: How many results to pull from each site before ranking. Sites order by
    #: their own idea of relevance, which for a multi-word title is "contains
    #: any of these words", so the real match is often not in the first few.
    #: Everything past the filter is discarded, so this costs one page load.
    CANDIDATE_POOL = 30

    def search(
        self,
        query: str,
        limit_per_site: int = 2,
        sites: list[str] | None = None,
    ) -> tuple[list[DirectVideo], dict[str, str]]:
        """Search every site at once, keeping only the best few from each.

        Returns the results and a map of site key to error message. One site
        being down is normal -- these are not services with uptime guarantees --
        and must not cost the user the other two, so failures are reported
        alongside the results rather than raised.

        Each site's results are ranked and filtered independently before being
        merged. Filtering after the merge would let one site that returns
        thirty loose matches crowd out another that returned two exact ones.
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
                executor.submit(scraper.search, query, self.CANDIDATE_POOL): key
                for key, scraper in selected.items()
            }
            for future in as_completed(futures):
                key = futures[future]
                try:
                    found = future.result()
                except Exception as exc:
                    logger.warning(f"Direct scraper {key} failed: {exc}")
                    errors[key] = str(exc)
                    results[key] = []
                    continue

                results[key] = best_matches(query, found, limit_per_site)
                logger.debug(
                    f"Direct scraper {key}: {len(found)} results, "
                    f"{len(results[key])} kept for {query!r}"
                )

        return _merge_ranked(selected, results), errors

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


def _merge_ranked(
    selected: dict[str, DirectScraper], results: dict[str, list[DirectVideo]]
) -> list[DirectVideo]:
    """Merge the per-site lists into one, best first.

    Ranked globally rather than round-robined between sites. Round-robin keeps
    every site visible, which mattered when each returned thirty results and
    the list was long enough to bury one; now that each contributes at most a
    couple, the whole list fits on screen and the only thing worth optimising
    for is that the best video is at the top.

    Position within a site breaks ties, so two results a site itself ranked in
    an order do not get shuffled out of it.
    """

    ranked: list[tuple[tuple, int, DirectVideo]] = []
    seen: set[str] = set()

    for key in selected:
        for position, video in enumerate(results.get(key) or []):
            if video.key() in seen:
                continue
            seen.add(video.key())
            ranked.append((sort_key(video), position, video))

    # Descending on quality, ascending on the site's own position, which is why
    # the two cannot be expressed as one reverse=True sort.
    ranked.sort(key=lambda entry: entry[1])
    ranked.sort(key=lambda entry: entry[0], reverse=True)

    return [video for _, _, video in ranked]


__all__ = ["DirectScraperService", "DirectSource", "DirectVideo"]
