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
from program.services.scrapers.results import ScrapeResult
from program.settings import settings_manager
from program.services.scrapers.adult_matching import MatchEvidence, evaluate
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
    results: dict[str, ScrapeResult],
    log_msg: bool = True,
    manual: bool = False,
) -> dict[str, Stream]:
    """Parse the results from the scrapers into Torrent objects.

    Args:
        item: The media item to parse results for.
        results: Dict mapping infohash to what the indexer reported.
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

    # RTN's `remove_trash` does two jobs at once: it drops releases matching
    # known trash patterns, and it drops releases whose parsed title is not
    # close enough to the one we asked for. The second is actively wrong for
    # adult releases. A scene filename leads with the site, so RTN parses
    # "PureTaboo.21.07.13.Vanna.Bardot.Deny.It.All.You.Want.XXX.1080p" as the
    # title "PureTaboo" and scores it 0.0 against "Deny It All You Want" --
    # discarding the best available release of that title before the adult
    # matcher, which scores the same release 9.5 on site, date, performer and
    # a 5/5 title match, ever gets to see it.
    #
    # So for adult items the title check is skipped and `data.trash` is
    # enforced by hand below, keeping the pattern filter while letting
    # `_filter_adult_torrents` be the thing that decides identity. It demands
    # corroboration from site, cast or date, which is a far stronger test than
    # a Levenshtein ratio against a filename that never contained the title in
    # the first place.
    adult_item = _is_adult_item(item)
    remove_trash = (
        False if (manual or adult_item) else active_settings.options["remove_all_trash"]
    )
    enforce_trash_flag = adult_item and not manual

    logger.debug(f"Processing {len(results)} results for {item.log_string}")

    for infohash, result in results.items():
        if infohash in processed_infohashes:
            continue

        raw_title = result.raw_title

        try:
            torrent = rtn_instance.rank(
                raw_title=raw_title,
                infohash=infohash,
                correct_title=correct_title,
                remove_trash=remove_trash,
                aliases=aliases,
            )

            if enforce_trash_flag and torrent.data.trash:
                # The half of remove_trash worth keeping for adult releases.
                logger.trace(
                    f"Skipping trash release for {item.log_string}: {raw_title}"
                )
                continue

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

        evidence_by_hash = dict[str, MatchEvidence]()

        if adult_item:
            torrents, evidence_by_hash = _filter_adult_torrents(item, torrents)

            if not torrents:
                logger.debug(
                    f"No release matched {item.log_string} on site, cast or date"
                )
                return {}

        sorted_torrents = sort_torrents(
            torrents,
            bucket_limit=scraping_settings.bucket_limit if not manual else 0,
        )

        ordered = list(sorted_torrents.values())

        if adult_item:
            ordered = _order_adult_torrents(ordered, evidence_by_hash)

        # Carry the indexer's own numbers onto the stream. `results` is keyed
        # by the infohash as the scraper gave it, which is not always the same
        # case as RTN's, so look both up rather than silently losing the data.
        def _reported(infohash: str) -> ScrapeResult | None:
            return results.get(infohash) or results.get(infohash.lower())

        torrent_stream_map = {
            torrent.infohash.lower(): Stream(torrent, _reported(torrent.infohash))
            for torrent in ordered
        }

        logger.debug(
            f"Kept {len(torrent_stream_map)} streams for {item.log_string} after processing bucket limit"
        )

        return torrent_stream_map

    return {}


# --- Adult relevance and ranking --------------------------------------------
#
# RTN scores mainstream quality markers: BluRay, DTS-HD, H.265, remux. Adult
# releases carry almost none of them, so rank alone is neither a usable
# ordering nor a safe filter.
#
# An earlier version of this compensated by accepting anything RTN flagged
# ``adult``. Measured against real Prowlarr output that was much worse than it
# sounds: for eight library titles it accepted 829 of 1386 results, of which a
# whole indexer's worth -- 283 unrelated JAV releases matching only the word
# "daddy" -- were noise. Relevance is now decided from the TPDB metadata the
# item actually carries: site, performers and release date.


def _is_adult_item(item: MediaItem) -> bool:
    """Whether this item is adult, and so expects adult releases.

    Covers both identifiers: an Adult Empire title has no TPDB id but is every
    bit as adult, and treating it as mainstream would apply the wrong match
    rules to its releases.
    """

    return bool(
        getattr(item, "tpdb_id", None) or getattr(item, "adultempire_id", None)
    )


def _match_evidence(item: MediaItem, torrent: Torrent) -> MatchEvidence:
    """Evidence linking one release to this item."""

    return evaluate(
        torrent.raw_title,
        item_title=item.title,
        site_name=getattr(item, "site_name", None),
        performers=list(getattr(item, "performers", None) or []),
        aired_at=getattr(item, "aired_at", None),
        is_adult_release=bool(getattr(torrent.data, "adult", False)),
    )


def _filter_adult_torrents(
    item: MediaItem, torrents: set[Torrent]
) -> tuple[set[Torrent], dict[str, MatchEvidence]]:
    """Keep only releases with corroborated evidence of being this title."""

    kept = set[Torrent]()
    evidence_by_hash = dict[str, MatchEvidence]()

    for torrent in torrents:
        evidence = _match_evidence(item, torrent)

        if evidence.accepted:
            kept.add(torrent)
            evidence_by_hash[torrent.infohash.lower()] = evidence
        else:
            logger.trace(
                f"Rejecting unrelated release for {item.log_string}: {torrent.raw_title}"
            )

    dropped = len(torrents) - len(kept)

    if dropped:
        logger.debug(
            f"Dropped {dropped} unrelated release(s) for {item.log_string}"
        )

    return kept, evidence_by_hash


def _order_adult_torrents(
    torrents: list[Torrent], evidence_by_hash: dict[str, MatchEvidence]
) -> list[Torrent]:
    """Order by how well a release is identified, then by quality.

    Evidence first: a release proven to be this scene beats a better-encoded
    one that merely might be. Resolution then decides between genuine matches,
    with RTN's rank as the final tie-break.
    """

    def key(torrent: Torrent) -> tuple[float, int, int]:
        evidence = evidence_by_hash.get(torrent.infohash.lower())
        resolution = getattr(torrent.data, "resolution", None) or ""

        return (
            evidence.score if evidence else 0.0,
            _RESOLUTION_ORDER.get(resolution, 0),
            getattr(torrent, "rank", 0) or 0,
        )

    return sorted(torrents, key=key, reverse=True)


# Higher is better. "unknown" sits below every known resolution but above
# nothing, because adult releases frequently omit it from the title entirely.
_RESOLUTION_ORDER = {
    "2160p": 5,
    "1080p": 4,
    "720p": 3,
    "540p": 2,
    "480p": 1,
}


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
