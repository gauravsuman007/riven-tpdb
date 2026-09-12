"""The OnlyFans scrapers, loaded from their own folder.

A second registry rather than a second folder on the existing one. The
discovery mechanism is generic -- `discover_plugins` only cares about a path --
but the two *sets* are not interchangeable, and merging them would be wrong in
both directions: an OnlyFans scraper would appear in the direct-play site list
and be run against library titles, and a tube scraper would appear in the
OnlyFans tab claiming to index performers.

Keeping them apart also means the two folders can be enabled, disabled and
imported into independently, which is what the separate settings tab is for.

The failure contract is the folder's, not this module's: a broken file is
recorded against its filename and skipped, so it shows up in the tab as a
broken plugin rather than as an absence.
"""

from dataclasses import dataclass

from loguru import logger

from program.services.directscrapers.base import DirectScraper
from program.services.directscrapers.plugins import discover_plugins
from program.settings import settings_manager


@dataclass(slots=True)
class ScraperInfo:
    """One scraper as the settings tab needs to describe it."""

    key: str
    name: str
    base_url: str
    enabled: bool
    source_file: str
    indexes_accounts: bool


class OnlyFansScraperRegistry:
    """Every scraper in the OnlyFans plugin folder, minus disabled ones."""

    def __init__(self) -> None:
        settings = settings_manager.settings.content.onlyfans

        self.plugin_dir = settings.plugin_dir
        disabled = set(settings.disabled)

        discovery = discover_plugins(self.plugin_dir)

        #: Filename -> what went wrong. Kept even when empty so the tab can say
        #: "no errors" rather than having to infer it.
        self.errors: dict[str, str] = dict(discovery.errors)
        self.sources: dict[str, str] = {
            key: loaded.source_file for key, loaded in discovery.plugins.items()
        }
        #: Every scraper found, including disabled ones -- the tab has to list
        #: a disabled scraper in order to offer re-enabling it.
        self.all: dict[str, DirectScraper] = {
            key: loaded.scraper for key, loaded in discovery.plugins.items()
        }
        self.services: dict[str, DirectScraper] = {
            key: scraper for key, scraper in self.all.items() if key not in disabled
        }

        if self.errors:
            logger.warning(
                f"OnlyFans scrapers: {len(self.errors)} file(s) failed to load"
            )
        logger.debug(
            f"OnlyFans scrapers: {len(self.services)} enabled "
            f"of {len(self.all)} in {self.plugin_dir}"
        )

    def describe(self) -> list[ScraperInfo]:
        """What the settings tab lists, disabled scrapers included."""

        return [
            ScraperInfo(
                key=key,
                name=getattr(scraper, "name", key),
                base_url=getattr(scraper, "base_url", ""),
                enabled=key in self.services,
                source_file=self.sources.get(key, ""),
                indexes_accounts=getattr(scraper, "indexes_accounts", False),
            )
            for key, scraper in sorted(self.all.items())
        ]


_registry: "OnlyFansScraperRegistry | None" = None


def registry() -> OnlyFansScraperRegistry:
    """The process-wide OnlyFans scraper registry.

    A singleton for the same reason the direct one is: rebuilding it per
    request would re-scan the folder and reset every site's connection pool.
    """

    global _registry

    if _registry is None:
        _registry = OnlyFansScraperRegistry()

    return _registry


def reset() -> None:
    """Drop the cached registry so a rescan, an import or a settings change
    takes effect without a restart."""

    global _registry
    _registry = None


__all__ = ["OnlyFansScraperRegistry", "ScraperInfo", "registry", "reset"]
