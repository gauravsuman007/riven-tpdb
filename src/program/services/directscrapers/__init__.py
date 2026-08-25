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
from program.services.directscrapers.eporner import EPornerScraper
from program.services.directscrapers.iporntv import IPornTVScraper
from program.services.directscrapers.models import DirectSource, DirectVideo
from program.services.directscrapers.ranking import (
    MatchTarget,
    best_matches,
    sort_key,
    strip_punctuation,
)
from program.services.directscrapers.tnaflix import TnaflixScraper
from program.services.directscrapers.upornia import UporniaScraper
from program.services.directscrapers.xfreehd import XFreeHDScraper


class DirectScraperService:
    """Runs every direct scraper against one query and merges the results."""

    key = "direct_scraping"

    #: Shown ahead of the other three regardless of relevance. Both are a
    #: bigger catalogue than the rest combined -- tnaflix alone answered
    #: every measured query, eporner has a documented API instead of scraped
    #: HTML -- so a user picking a source is better served starting here.
    PRIORITY_SITES = frozenset({"tnaflix", "eporner"})

    def __init__(self) -> None:
        self.services: dict[str, DirectScraper] = {
            scraper.key: scraper
            for scraper in (
                TnaflixScraper(),
                EPornerScraper(),
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
        target: MatchTarget | str,
        limit_per_site: int = 3,
        sites: list[str] | None = None,
    ) -> tuple[list[DirectVideo], dict[str, str]]:
        """Search every site at once, keeping only the best few from each.

        Returns the results and a map of site key to error message. One site
        being down is normal -- these are not services with uptime guarantees --
        and must not cost the user the rest, so failures are reported
        alongside the results rather than raised.

        Each site is searched with several phrasings rather than one, because
        the sites do not agree on what a query means. xfreehd ANDs the terms,
        so a full scene title matches nothing and only a short "series +
        performer" finds anything; every other site ORs them, so every extra
        word adds noise instead. One phrasing cannot serve both, and measuring
        showed each site's hit arriving under a different one.

        Results are pooled across phrasings, then ranked and filtered per site.
        Filtering after the merge would let one site returning thirty loose
        matches crowd out another that returned two exact ones.
        """

        if isinstance(target, str):
            target = MatchTarget.build(target)

        selected = {
            key: scraper
            for key, scraper in self.services.items()
            if not sites or key in sites
        }
        queries = self.query_ladder(target)

        results: dict[str, list[DirectVideo]] = {key: [] for key in selected}
        errors: dict[str, str] = {}
        jobs = [(key, query) for key in selected for query in queries]

        with ThreadPoolExecutor(max_workers=min(len(jobs), 8) or 1) as executor:
            futures = {
                executor.submit(
                    selected[key].search, query, self.CANDIDATE_POOL
                ): (key, query)
                for key, query in jobs
            }
            for future in as_completed(futures):
                key, query = futures[future]
                try:
                    results[key].extend(future.result())
                except Exception as exc:
                    logger.warning(
                        f"Direct scraper {key} failed for {query!r}: {exc}"
                    )
                    # Only the last failure is kept: the UI reports that a site
                    # could not be reached, not which phrasing tripped it.
                    errors[key] = str(exc)

        for key, found in results.items():
            pooled = _unique_by_id(found)
            results[key] = best_matches(target, pooled, limit_per_site)
            logger.debug(
                f"Direct scraper {key}: {len(pooled)} candidates across "
                f"{len(queries)} queries, {len(results[key])} kept "
                f"for {target.title!r}"
            )
            # A site that answered every phrasing is not in trouble, whatever
            # one of them did.
            if results[key]:
                errors.pop(key, None)

        return _merge_ranked(selected, results), errors

    @staticmethod
    def query_ladder(target: MatchTarget) -> list[str]:
        """The phrasings to try, in descending order of specificity.

        Measured against the library rather than guessed. The studio was
        dropped: TPDB records the network ("Nubiles", "Diabolic Video") while
        uploads are labelled with the series, so pairing it with a performer
        never once surfaced the target and cost a request every time.
        """

        lead = target.performers[0] if target.performers else ""
        candidates = [
            target.title,
            f"{strip_punctuation(target.title)} {lead}".strip(),
            # The series on its own, not paired with a performer. The library
            # lists a scene's cast alphabetically, and the first name is rarely
            # the one a tube upload puts in its title -- searching "Bratty Sis"
            # finds the series' scenes and lets the ranker pick out whichever
            # of the credited cast actually appears.
            target.series,
        ]

        ladder: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            candidate = candidate.strip()
            key = candidate.casefold()
            if candidate and key not in seen:
                seen.add(key)
                ladder.append(candidate)
        return ladder

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


def _unique_by_id(videos: list[DirectVideo]) -> list[DirectVideo]:
    """Collapse the same video arriving from more than one phrasing."""

    unique: list[DirectVideo] = []
    seen: set[str] = set()
    for video in videos:
        if video.key() in seen:
            continue
        seen.add(video.key())
        unique.append(video)
    return unique


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
    an order do not get shuffled out of it. A priority site's tier of 0 always
    sorts ahead of a non-priority site's tier of 1, however the quality
    compares -- that ordering is a deliberate site preference, not something
    relevance should be allowed to override.
    """

    ranked: list[tuple[int, tuple, int, DirectVideo]] = []
    seen: set[str] = set()

    for key in selected:
        tier = 0 if key in DirectScraperService.PRIORITY_SITES else 1
        for position, video in enumerate(results.get(key) or []):
            if video.key() in seen:
                continue
            seen.add(video.key())
            ranked.append((tier, sort_key(video), position, video))

    # Each sort is stable and applied least-significant first, so the final
    # order is: tier ascending, then quality descending, then the site's own
    # position ascending.
    ranked.sort(key=lambda entry: entry[2])
    ranked.sort(key=lambda entry: entry[1], reverse=True)
    ranked.sort(key=lambda entry: entry[0])

    return [video for _, _, _, video in ranked]


__all__ = ["DirectScraperService", "DirectSource", "DirectVideo", "MatchTarget"]
