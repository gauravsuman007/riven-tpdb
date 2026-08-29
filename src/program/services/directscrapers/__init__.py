"""Direct streaming-site search.

Separate from ``program.services.scrapers`` on purpose. Those find torrents,
which go through ranking, a debrid provider and the VFS before anything can be
played. These find a file on a website and play it -- no infohash, no download,
nothing added to the library. Sharing a base class would have forced one of the
two into the other's shape.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from loguru import logger

from program.services.directscrapers.base import DirectScraper
from program.services.directscrapers.models import DirectSource, DirectVideo
from program.services.directscrapers.plugins import discover_plugins
from program.services.directscrapers.ranking import (
    MatchTarget,
    best_matches,
    sort_key,
    strip_punctuation,
)


@dataclass(slots=True)
class ScraperInfo:
    """One scraper as the Plugins tab needs to show it, whether or not it is
    currently switched on."""

    key: str
    name: str
    base_url: str
    kind: str  # "builtin" | "plugin"
    enabled: bool
    source_file: str | None = None
    error: str | None = None


def describe_scrapers() -> list[ScraperInfo]:
    """Every known scraper, enabled or not -- built-in first, then plugins.

    Separate from `DirectScraperService.services`, which holds only what is
    actually enabled: the Plugins tab needs to show and re-enable something
    that is currently switched off, and a load error has to surface even
    though nothing was actually registered for it.
    """

    # Imported here, not at module scope: settings pulls in a large part
    # of the application (RTN, the DB models, ...), and this module is
    # imported by test suites that stub those out deliberately.
    from program.settings import settings_manager

    settings = settings_manager.settings.direct_scraping
    disabled = set(settings.disabled)
    infos: list[ScraperInfo] = []

    discovery = discover_plugins(settings.plugin_dir)

    for key, loaded in discovery.plugins.items():
        scraper = loaded.scraper
        infos.append(
            ScraperInfo(
                key=key,
                name=scraper.name,
                base_url=scraper.base_url,
                kind="plugin",
                enabled=key not in disabled,
                source_file=loaded.source_file,
            )
        )

    for filename, error in discovery.errors.items():
        infos.append(
            ScraperInfo(
                key=f"error:{filename}",
                name=filename,
                base_url="",
                kind="plugin",
                enabled=False,
                source_file=filename,
                error=error,
            )
        )

    return infos


class DirectScraperService:
    """Runs every direct scraper against one query and merges the results."""

    key = "direct_scraping"

    #: Sites are shown in tiers, ahead of relevance rather than ranked purely
    #: by it: a lower tier always outranks a higher one, whatever the two
    #: sites' scores say, and tier is a deliberate site preference rather than
    #: something measured. Tier 0 is the biggest catalogue and the least
    #: scraped (tnaflix answered every measured query; eporner has a
    #: documented API). Tier 1 fills out the middle. Anything not listed here
    #: sorts last, by relevance as before.
    #:
    #: These are only the FALLBACK. `direct_scraping.site_order`, set from the
    #: Plugins tab, takes precedence when it is non-empty -- see
    #: `site_tier()`. They still matter for a fresh install, where nothing has
    #: been reordered yet.
    SITE_TIERS: dict[str, int] = {
        "tnaflix": 0,
        "eporner": 0,
        "hqporner": 1,
        "paradisehill": 1,
        "tubepornclassic": 1,
    }

    def __init__(self) -> None:
        self.plugin_sources: dict[str, str] = {}
        self.plugin_errors: dict[str, str] = {}
        self.services: dict[str, DirectScraper] = self._load_all()
        self.initialized = True

    def _load_all(self) -> dict[str, DirectScraper]:
        """Every scraper dropped into the plugin folder, minus disabled ones.

        Every site scraper is a plugin -- see the `riven-tpdb-scrapers` repo,
        mounted into `plugin_dir` at deploy time. None are bundled with this
        image: a scraper is a bet on one site's markup staying stable, which
        is a maintenance burden this codebase should not carry, and the two
        were never actually different in shape -- both were `DirectScraper`
        subclasses discovered the same way.
        """

        from program.settings import settings_manager

        settings = settings_manager.settings.direct_scraping
        disabled = set(settings.disabled)

        discovery = discover_plugins(settings.plugin_dir)
        self.plugin_sources = {
            key: loaded.source_file for key, loaded in discovery.plugins.items()
        }
        self.plugin_errors = dict(discovery.errors)

        return {
            key: loaded.scraper
            for key, loaded in discovery.plugins.items()
            if key not in disabled
        }

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


def site_tier(key: str) -> int:
    """How early this site's results sort, lower first.

    A user-stated order wins outright over the built-in defaults: someone who
    moves fpoxxx to the top means it, and having a measured default quietly
    outrank that would make the control look broken. Sites the user has not
    placed sort after every site they have, so a newly-dropped plugin cannot
    silently land above a deliberate choice.
    """

    from program.settings import settings_manager

    order = settings_manager.settings.direct_scraping.site_order

    if order:
        try:
            return order.index(key)
        except ValueError:
            return len(order)

    return DirectScraperService.SITE_TIERS.get(key, 2)


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
    an order do not get shuffled out of it. A lower tier always sorts ahead of
    a higher one, however the quality compares -- that ordering is a
    deliberate site preference, not something relevance should be allowed to
    override.
    """

    ranked: list[tuple[int, tuple, int, DirectVideo]] = []
    seen: set[str] = set()

    for key in selected:
        tier = site_tier(key)
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


_service: "DirectScraperService | None" = None


def service() -> "DirectScraperService":
    """The process-wide scraper registry.

    A singleton for the same reason as VPN: rebuilding one per request would
    reset connection pooling for every site, and would re-scan the plugin
    folder on every search.
    """

    global _service

    if _service is None:
        _service = DirectScraperService()

    return _service


def reset() -> None:
    """Drop the cached service so a settings change or a new plugin file
    takes effect without a restart."""

    global _service
    _service = None


__all__ = [
    "DirectScraperService",
    "DirectSource",
    "DirectVideo",
    "MatchTarget",
    "ScraperInfo",
    "describe_scrapers",
    "reset",
    "service",
]
