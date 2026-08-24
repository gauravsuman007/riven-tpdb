"""Shared functions for scrapers."""

import regex
from loguru import logger
from RTN import (
    RTN,
    ParsedData,
    Torrent,
    sort_torrents,
    BaseRankingModel,
    DefaultRanking,
)
from RTN.models import SettingsModel
from typing import cast

from program.media.item import Episode, MediaItem, Movie, Season, Show
from program.media.stream import Stream
from program.settings import settings_manager
from program.settings.models import RTNSettingsModel, ScraperModel

scraping_settings: ScraperModel = settings_manager.settings.scraping
ranking_settings: RTNSettingsModel = settings_manager.settings.ranking
ranking_model: BaseRankingModel = DefaultRanking()
rtn = RTN(ranking_settings, ranking_model)


def get_ranking_overrides(
    ranking_overrides: dict[str, list[str]] | None,
) -> SettingsModel | None:
    if not ranking_overrides:
        return None

    try:
        # Create a deep copy of current settings
        settings_model = RTNSettingsModel(**ranking_settings.model_dump())

        # Collect groups: resolutions + all custom rank categories
        groups = [("resolutions", settings_model.resolutions)]
        if hasattr(settings_model.custom_ranks, "__class__"):
            groups.extend(
                (cat, val)
                for cat in settings_model.custom_ranks.__class__.model_fields
                if (val := getattr(settings_model.custom_ranks, cat)) is not None
            )

        for category, obj in groups:
            if category not in ranking_overrides:
                continue

            if not obj.__class__.model_fields:
                continue

            targets = set(ranking_overrides[category])
            
            # Iterate fields (assuming Pydantic model)
            for key in obj.__class__.model_fields:
                if key == "unknown":
                    continue

                should_enable = key in targets
                val = getattr(obj, key)

                if isinstance(val, bool):
                    setattr(obj, key, should_enable)
                elif hasattr(val, "fetch"):
                    val.fetch = should_enable

        return settings_model
    except Exception as e:
        logger.error(f"Failed to apply ranking overrides: {e}")
        return None


# "Vol. 3", "Volume 3", "Part 3" -- the edition marker that distinguishes one
# release in a series from another.
_VOLUME_PATTERN = regex.compile(
    r"\b(?:vol(?:ume)?|part|pt)\.?\s*(\d{1,3})\b", regex.IGNORECASE
)


def _volume_number(title: str) -> int | None:
    """Return the volume/part number in a title, if it carries one."""

    match = _VOLUME_PATTERN.search(title or "")

    return int(match.group(1)) if match else None


def _volume_mismatch(correct_title: str, raw_title: str) -> bool:
    """True when both titles name a volume and the volumes differ.

    RTN normalises the volume away, so "Brazzers University Vol. 2" and
    "Brazzers University Vol. 4" compare as the same title -- at the looser
    similarity threshold adult releases need, one volume happily satisfies a
    request for another. Only reject when *both* sides state a volume; a
    release that simply omits it stays eligible.
    """

    wanted = _volume_number(correct_title)

    if wanted is None:
        return False

    found = _volume_number(raw_title)

    return found is not None and found != wanted


def parse_results(
    item: MediaItem,
    results: dict[str, str],
    log_msg: bool = True,
    manual: bool = False,
) -> dict[str, Stream]:
    """Parse the results from the scrapers into Torrent objects.

    Args:
        item: The media item to parse results for.
        results: Dict mapping infohash to raw title.
        manual: If True, bypass content filters (for manual scraping).
    """

    torrents = set[Torrent]()
    processed_infohashes = set[str]()
    correct_title = item.top_title


    # Use effective RTN settings (handles explicit overrides/context implicitly)
    active_settings = settings_manager.get_effective_rtn_model()
    
    # Check if we are diverging from the global singleton `rtn` instance
    is_default_settings = (active_settings.model_dump() == ranking_settings.model_dump())
    
    if is_default_settings:
        rtn_instance = rtn
    else:
        rtn_instance = RTN(active_settings, ranking_model)

    aliases = (
        {k: v for k, v in a.items() if k not in active_settings.languages.exclude}
        if scraping_settings.enable_aliases and (a := item.get_aliases())
        else {}
    )

    logger.debug(f"Processing {len(results)} results for {item.log_string}")

    for infohash, raw_title in results.items():
        if infohash in processed_infohashes:
            continue

        try:
            torrent = rtn_instance.rank(
                raw_title=raw_title,
                infohash=infohash,
                correct_title=correct_title,
                remove_trash=active_settings.options[
                    "remove_all_trash"
                ] if not manual else False,
                aliases=aliases,
            )

            if isinstance(item, Movie):
                # If movie item, disregard torrents with seasons and episodes
                if not manual and (torrent.data.episodes or torrent.data.seasons):
                    logger.trace(
                        f"Skipping show torrent for movie {item.log_string}: {raw_title}"
                    )
                    continue

            if not manual and _volume_mismatch(correct_title, raw_title):
                logger.debug(
                    f"Skipping wrong volume for {item.log_string}: {raw_title}"
                )
                continue

            if isinstance(item, Show):
                # make sure the torrent has at least 2 episodes (should weed out most junk)
                if not manual and torrent.data.episodes and len(torrent.data.episodes) <= 2:
                    logger.trace(
                        f"Skipping torrent with too few episodes for {item.log_string}: {raw_title}"
                    )
                    continue

                # make sure all of the item seasons are present in the torrent
                if not manual and not all(
                    season.number in torrent.data.seasons for season in item.seasons
                ):
                    logger.trace(
                        f"Skipping torrent with incorrect number of seasons for {item.log_string}: {raw_title}"
                    )
                    continue

                if (
                    not manual
                    and torrent.data.episodes
                    and not torrent.data.seasons
                    and len(item.seasons) == 1
                    and not all(
                        episode.number in torrent.data.episodes
                        for episode in item.seasons[0].episodes
                    )
                ):
                    logger.trace(
                        f"Skipping torrent with incorrect number of episodes for {item.log_string}: {raw_title}"
                    )
                    continue

            if isinstance(item, Season):
                if not manual and torrent.data.seasons and item.number not in torrent.data.seasons:
                    logger.trace(
                        f"Skipping torrent with no seasons or incorrect season number for {item.log_string}: {raw_title}"
                    )
                    continue

                # make sure the torrent has at least 2 episodes (should weed out most junk)
                if not manual and torrent.data.episodes and len(torrent.data.episodes) <= 2:
                    logger.trace(
                        f"Skipping torrent with too few episodes for {item.log_string}: {raw_title}"
                    )
                    continue

                # disregard torrents with incorrect season number
                if not manual and item.number not in torrent.data.seasons:
                    logger.trace(
                        f"Skipping incorrect season torrent for {item.log_string}: {raw_title}"
                    )
                    continue

                if not manual and torrent.data.episodes and not all(
                    episode.number in torrent.data.episodes for episode in item.episodes
                ):
                    logger.trace(
                        f"Skipping incorrect season torrent for not having all episodes {item.log_string}: {raw_title}"
                    )
                    continue

            if isinstance(item, Episode) and not manual:
                # Disregard torrents with incorrect episode number logic:
                skip = False

                # If the torrent has episodes, but the episode number is not present
                if torrent.data.episodes:
                    if (
                        item.number not in torrent.data.episodes
                        and item.absolute_number not in torrent.data.episodes
                    ):
                        skip = True

                # If the torrent does not have episodes, but has seasons, and the parent season is not present
                elif torrent.data.seasons:
                    # item is confirmed to be Episode at line 197
                    # Episode.parent is a Season, and Season has a 'number' attribute
                    parent_season = cast(Season, item.parent)
                    if parent_season.number not in torrent.data.seasons:
                        skip = True

                # If the torrent has neither episodes nor seasons, skip (junk)
                else:
                    skip = True

                if skip:
                    logger.trace(
                        f"Skipping incorrect episode torrent for {item.log_string}: {raw_title}"
                    )
                    continue

            if not manual and torrent.data.country and not item.is_anime:
                # If country is present, then check to make sure it's correct. (Covers: US, UK, NZ, AU)
                if (
                    torrent.data.country
                    and (item_country := _get_item_country(item))
                    and torrent.data.country not in item_country
                ):
                    logger.trace(
                        f"Skipping torrent for incorrect country with {item.log_string}: {raw_title}"
                    )
                    continue

            if (
                not manual
                and torrent.data.year
                and item.aired_at
                and not _check_item_year(item, torrent.data)
            ):
                # If year is present, then check to make sure it's correct
                logger.trace(
                    f"Skipping torrent for incorrect year with {item.log_string}: {raw_title}"
                )
                continue

            if not manual and item.is_anime and scraping_settings.dubbed_anime_only:
                # If anime and user wants dubbed only, then check to make sure it's dubbed
                if not torrent.data.dubbed:
                    logger.trace(
                        f"Skipping non-dubbed anime torrent for {item.log_string}: {raw_title}"
                    )
                    continue

            torrents.add(torrent)
            processed_infohashes.add(infohash)
        except Exception as e:
            logger.trace(f"GarbageTorrent: {e}")
            processed_infohashes.add(infohash)
            continue

    if torrents:
        logger.debug(f"Found {len(torrents)} streams for {item.log_string}")

        if _is_adult_item(item):
            torrents = _filter_adult_torrents(item, torrents)

            if not torrents:
                return {}

        sorted_torrents = sort_torrents(
            torrents,
            bucket_limit=scraping_settings.bucket_limit if not manual else 0,
        )

        ordered = list(sorted_torrents.values())

        if _is_adult_item(item):
            ordered = _order_adult_torrents(ordered)

        torrent_stream_map = {
            torrent.infohash.lower(): Stream(torrent) for torrent in ordered
        }

        logger.debug(
            f"Kept {len(torrent_stream_map)} streams for {item.log_string} after processing bucket limit"
        )

        return torrent_stream_map

    return {}


# --- Adult ranking -----------------------------------------------------------
#
# RTN scores mainstream quality markers: BluRay, DTS-HD, H.265, remux. Adult
# releases carry almost none of them, so a scrape for an adult title ranked an
# unrelated mainstream film ("Fight.Club.1999.1080p.BluRay...") at 2600 while
# the correct releases sat at 0. Rank alone is therefore not a usable ordering
# here, and it is also not safe as a filter.

# How close a non-adult release's title must be before it is believed. Adult
# titles collide with mainstream ones often ("Daddy Issues", "Alpha Male"), so
# a release that is not flagged adult has to match almost exactly to survive.
_ADULT_TITLE_MATCH_FLOOR = 0.85


def _is_adult_item(item: MediaItem) -> bool:
    """Whether this item came from TPDB, and so expects adult releases."""

    return bool(getattr(item, "tpdb_id", None))


def _torrent_similarity(torrent: Torrent) -> float:
    """RTN's title match for a torrent, 0.0 when it did not record one."""

    try:
        return float(getattr(torrent, "lev_ratio", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _filter_adult_torrents(item: MediaItem, torrents: set[Torrent]) -> set[Torrent]:
    """Drop mainstream collisions from an adult item's results.

    A release parsed as adult is kept regardless of rank. Anything else has to
    clear a high title-similarity bar, which is what separates "Daddy Issues 8"
    the adult title from the 2018 film that shares its name.
    """

    kept = set[Torrent]()

    for torrent in torrents:
        if getattr(torrent.data, "adult", False):
            kept.add(torrent)
            continue

        if _torrent_similarity(torrent) >= _ADULT_TITLE_MATCH_FLOOR:
            kept.add(torrent)
            continue

        logger.trace(
            f"Skipping non-adult release for {item.log_string}: {torrent.raw_title}"
        )

    dropped = len(torrents) - len(kept)

    if dropped:
        logger.debug(
            f"Dropped {dropped} non-adult release(s) for {item.log_string}"
        )

    return kept


def _order_adult_torrents(torrents: list[Torrent]) -> list[Torrent]:
    """Re-order an adult item's results so relevance beats mainstream polish.

    Adult-flagged releases first, then how well the title matched, and only
    then RTN's rank -- which still does useful work as a tie-break between two
    releases of the same title.
    """

    return sorted(
        torrents,
        key=lambda torrent: (
            bool(getattr(torrent.data, "adult", False)),
            _torrent_similarity(torrent),
            getattr(torrent, "rank", 0) or 0,
        ),
        reverse=True,
    )


# helper functions


def _check_item_year(item: MediaItem, data: ParsedData) -> bool:
    """Check if the year of the torrent is within the range of the item or its top-level parent."""
    
    valid_years: set[int] = set()

    if item.aired_at:
        valid_years.update([
            item.aired_at.year - 1,
            item.aired_at.year,
            item.aired_at.year + 1,
        ])

    # Also check the top-level parent's release year, since many show torrents use the premiere year (e.g., Lucifer (2016) S04)
    if isinstance(item, (Season, Episode)):
        top_parent: Show = item.top_parent
        if top_parent.aired_at:
            valid_years.update([
                top_parent.aired_at.year - 1,
                top_parent.aired_at.year,
                top_parent.aired_at.year + 1,
            ])

    return data.year in valid_years


def _get_item_country(item: MediaItem) -> str | None:
    """Get the country code for a country."""

    country = None

    if isinstance(item, Season) and item.parent.country:
        country = item.parent.country.upper()
    elif isinstance(item, Episode) and item.parent.parent.country:
        country = item.parent.parent.country.upper()
    elif item.country:
        country = item.country.upper()

    if not country:
        return None

    # need to normalize
    if country == "USA":
        country = "US"
    elif country == "GB":
        country = "UK"

    return country
