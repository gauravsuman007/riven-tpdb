"""Jackett scraper module"""

import concurrent.futures

from loguru import logger
from pydantic import BaseModel, Field
from requests import ReadTimeout

from program.media.item import MediaItem, Movie
from program.services.scrapers.base import ScraperService
from program.services.scrapers.results import ScrapeResult
from program.settings import settings_manager
from program.utils.request import SmartSession, get_hostname_from_url
from program.utils.torrent import extract_infohash, normalize_infohash
from program.settings.models import JackettConfig


class JackettScrapeResponse(BaseModel):
    """Model for Jackett scrape response"""

    class JackettTorrentResult(BaseModel):
        """Model for a single Jackett torrent result"""

        title: str = Field(alias="Title")
        link: str | None = Field(alias="Link")
        info_hash: str | None = Field(alias="InfoHash")
        magnet_uri: str | None = Field(alias="MagnetUri")
        # Torznab reports these on most indexers but not all, so every one is
        # optional and stays unknown rather than defaulting to zero.
        seeders: int | None = Field(alias="Seeders", default=None)
        leechers: int | None = Field(alias="Peers", default=None)
        size: int | None = Field(alias="Size", default=None)
        tracker: str | None = Field(alias="Tracker", default=None)

    results: list[JackettTorrentResult] = Field(alias="Results")


class Jackett(ScraperService[JackettConfig]):
    """Scraper for `Jackett`"""

    def __init__(self):
        super().__init__()

        self.api_key = None
        self.indexers = None
        self.settings = settings_manager.settings.scraping.jackett
        self.request_handler = None
        self._initialize()

    def validate(self) -> bool:
        """Validate Jackett settings."""

        if not self.settings.enabled:
            return False

        if self.settings.url and self.settings.api_key:
            self.api_key = self.settings.api_key

            try:
                if self.settings.timeout <= 0:
                    logger.error("Jackett timeout must be a positive integer")
                    return False

                self.session = SmartSession(
                    base_url=f"{self.settings.url.rstrip('/')}/api/v2.0",
                    rate_limits=(
                        {
                            get_hostname_from_url(self.settings.url): {
                                "rate": 300 / 60,
                                "capacity": 300,
                            }
                        }
                        if self.settings.ratelimit
                        else None
                    ),
                    retries=self.settings.retries,
                    backoff_factor=0.3,
                )

                return True
            except ReadTimeout:
                logger.error(
                    "Jackett request timed out. Check your indexers, they may be too slow to respond."
                )
                return False
            except Exception as e:
                logger.error(f"Jackett failed to initialize with API Key: {e}")
                return False

        logger.warning("Jackett is not configured and will not be used.")

        return False

    def run(self, item: MediaItem) -> dict[str, ScrapeResult]:
        """
        Scrape the Jackett site for the given media items
        and update the object with scraped streams
        """

        try:
            return self.scrape(item)
        except Exception as e:
            if "rate limit" in str(e).lower() or "429" in str(e):
                logger.debug(f"Jackett ratelimit exceeded for item: {item.log_string}")
                return {}

            logger.error(f"Jackett failed to scrape item with error: {e}")

            # Re-raise so scrapers/__init__.py's run_service_streaming can
            # surface this as a real failure rather than "0 streams found",
            # which is what a genuine empty result also looks like.
            raise

    def scrape(self, item: MediaItem) -> dict[str, ScrapeResult]:
        """Scrape the given media item"""

        torrents = dict[str, ScrapeResult]()
        query = item.log_string

        # Adult scenes are indexed by exact title; appending the release year
        # only hurts matching on adult trackers.
        if isinstance(item, Movie) and item.aired_at and not item.is_adult:
            query = f"{query} ({item.aired_at.year})"

        logger.debug(f"Searching for '{query}' in Jackett")

        response = f"/indexers/test:passed/results?apikey={self.api_key}&Query={query}"
        response = self.session.get(response, timeout=self.settings.timeout)

        if not response.ok:
            return torrents

        data = JackettScrapeResponse.model_validate(response.json())

        if data.results:
            # list of (result, title) tuples that need URL fetching
            urls_to_fetch = list[
                tuple[JackettScrapeResponse.JackettTorrentResult, str]
            ]()

            def described(
                result: JackettScrapeResponse.JackettTorrentResult,
            ) -> ScrapeResult:
                """What Jackett reported about a release."""

                return ScrapeResult(
                    raw_title=result.title,
                    seeders=result.seeders,
                    # Torznab's "Peers" counts everyone in the swarm, seeders
                    # included, so the leecher count is the difference.
                    leechers=(
                        max(result.leechers - (result.seeders or 0), 0)
                        if result.leechers is not None
                        else None
                    ),
                    size=result.size,
                    indexer=result.tracker,
                )

            # First pass: extract infohashes from available fields and collect URLs that need fetching
            for result in data.results:
                infohash = None

                # Priority 1: Use InfoHash field directly if available (normalize to handle base32)
                if result.info_hash:
                    infohash = normalize_infohash(result.info_hash)

                # Priority 2: Check if MagnetUri is available and extract from it
                if not infohash and result.magnet_uri:
                    infohash = extract_infohash(result.magnet_uri)

                # Priority 3: Collect URLs that need fetching
                if not infohash and result.link:
                    urls_to_fetch.append((result, result.title))

                elif infohash:
                    # We already have an infohash, add it directly
                    torrents[infohash] = described(result)

            # Fetch URLs in parallel
            if urls_to_fetch:
                with concurrent.futures.ThreadPoolExecutor(
                    thread_name_prefix="JackettHashExtract", max_workers=10
                ) as executor:
                    future_to_result = {
                        executor.submit(self.get_infohash_from_url, result.link): (
                            result,
                            title,
                        )
                        for result, title in urls_to_fetch
                        if result.link
                    }

                    done, pending = concurrent.futures.wait(
                        future_to_result.keys(),
                        timeout=self.settings.infohash_fetch_timeout,
                    )

                    # Process completed futures
                    for future in done:
                        result, title = future_to_result[future]

                        try:
                            infohash = future.result()

                            if infohash:
                                torrents[infohash] = described(result)
                        except Exception as e:
                            logger.debug(
                                f"Failed to get infohash from Link for {title}: {e}"
                            )

                    # Cancel and log timeouts for pending futures
                    for future in pending:
                        result, title = future_to_result[future]

                        future.cancel()

                        logger.debug(f"Timeout getting infohash from Link for {title}")

        if torrents:
            logger.log(
                "SCRAPER", f"Found {len(torrents)} streams for {item.log_string}"
            )
        else:
            logger.log("NOT_FOUND", f"No streams found for {item.log_string}")
        return torrents
