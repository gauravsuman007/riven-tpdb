"""Resolving a title against whichever metadata provider can answer.

One entry point, `resolve_movie`, replacing the direct call into
`tpdb_lookup.resolve_movie`. It walks the providers in the configured order
and returns the first acceptable match.

What counts as "move on to the next provider"
---------------------------------------------
Both of these, deliberately:

  * **No acceptable match.** The common case, and the reason this exists. A
    provider not having a record is normal -- TPDB has no entry for plenty of
    older releases, and its text search ranks its own way, so the right record
    can sit outside the results entirely. "Babysitters" (Digital Playground,
    2007) is the worked example: it only surfaces on TPDB once the studio is
    part of the query, and a library built without that fallback ends up
    holding a 2025 GenderX scene instead.

  * **The provider failed.** No API key, unreachable, rate limited, a 500, a
    GraphQL error. A provider that cannot answer must not end the search --
    that is precisely when the other one is worth having. Failures are logged
    once per lookup rather than raised, because a title that resolves from the
    second provider is a success, not a degraded one.

A provider that is disabled or unconfigured is SKIPPED, not counted as a
failure: it has not been tried, and reporting an error for something the user
simply has not set up sends them looking for a fault that is not there.
"""

from __future__ import annotations

from kink import di
from loguru import logger

from program.apis.stashdb_api import StashdbApi, StashdbApiError
from program.apis.tpdb_api import TpdbApi
from program.services.awards.matching import Match, evaluate_candidate, best_match
from program.services.indexers.stashdb_mapping import scene_to_movie_dict
from program.services.recommendations import adultempire_lookup, tpdb_lookup
from program.services.recommendations.adultempire import AdultEmpireClient
from program.settings import settings_manager

# StashDB's search is a single fuzzy `searchScenes` call that already returns
# full records -- there is no flat/detail split to work around as there is on
# TPDB -- so every hit can be scored directly and a wider net costs one
# request rather than one per candidate.
STASHDB_SEARCH_LIMIT = 15


# One client for the process. It serialises its own requests and sleeps
# between them, which is the whole point -- a per-lookup client would have no
# memory of the last request and would defeat the rate limit.
_adultempire_client = AdultEmpireClient()


def _provider_order() -> list[str]:
    return list(settings_manager.settings.metadata.providers or ["tpdb"])


def _tpdb_enabled() -> bool:
    settings = settings_manager.settings.tpdb

    return bool(settings.enabled and settings.api_token)


def _resolve_via_tpdb(**kwargs) -> Match | None:
    api = di[TpdbApi]

    return tpdb_lookup.resolve_movie(api, **kwargs)


def _adultempire_enabled() -> bool:
    return bool(settings_manager.settings.adultempire_metadata.enabled)


def _resolve_via_adultempire(
    *,
    title: str,
    studio: str | None = None,
    year: int | None = None,
    performers: list[str] | None = None,
    year_offset: int = 0,
) -> Match | None:
    """The best acceptable Adult Empire title, or None.

    Returns None rather than building the index when it is missing or stale.
    Building takes roughly twenty minutes of rate-limited crawling, and doing
    that inside a lookup would stall whatever is waiting on it and then do it
    again on the next call. The scheduled job owns the build; this only reads.
    """

    settings = settings_manager.settings.adultempire_metadata
    entries = adultempire_lookup.load_index(
        settings.index_path, settings.index_max_age_days
    )

    if not entries:
        logger.debug(
            "Adult Empire index is missing or stale; skipping. It is built by "
            "the scheduled job, or manually with build_adultempire_index()."
        )
        return None

    candidates = list[Match]()

    for entry in adultempire_lookup.rank_candidates(entries, title):
        detail = adultempire_lookup.fetch_detail(_adultempire_client, entry)

        if detail is None or not detail.title:
            continue

        candidates.append(
            evaluate_candidate(
                entry_title=title,
                entry_studio=studio,
                entry_year=year,
                year_offset=year_offset,
                entry_performers=list(performers or []),
                tpdb_id=detail.product_id,
                tpdb_kind="movie",
                tpdb_title=detail.title,
                tpdb_site=detail.studio,
                tpdb_date=f"{detail.year}-01-01" if detail.year else None,
                tpdb_performers=list(detail.performers or []),
                tpdb_poster=detail.poster,
            )
        )

    match = best_match(candidates)

    if match is not None:
        match.provider = "adultempire"

    return match


def _resolve_via_stashdb(
    *,
    title: str,
    studio: str | None = None,
    year: int | None = None,
    performers: list[str] | None = None,
    year_offset: int = 0,
) -> Match | None:
    """The best acceptable StashDB scene for this title, or None."""

    api = di[StashdbApi]

    # Searched with the studio as well as without, for the same reason the
    # TPDB lookup does: a bare common title returns the wrong studio's scenes
    # and the right record never appears in the results at all, at which point
    # no amount of scoring can recover it.
    queries = [title]

    if studio:
        queries.append(f"{title} {studio}")

    seen = set[str]()
    scenes = list[dict]()

    for term in queries:
        for scene in api.search_scenes(term, limit=STASHDB_SEARCH_LIMIT):
            scene_id = scene.get("id")

            if scene_id and scene_id not in seen:
                seen.add(scene_id)
                scenes.append(scene)

    candidates = list[Match]()

    for scene in scenes:
        mapped = scene_to_movie_dict(scene)
        aired_at = mapped["aired_at"]

        candidates.append(
            evaluate_candidate(
                entry_title=title,
                entry_studio=studio,
                entry_year=year,
                year_offset=year_offset,
                entry_performers=list(performers or []),
                # The scorer's parameters are named for TPDB because it was
                # the only provider; the scoring itself is provider-agnostic
                # (title, studio, year, cast). `provider` below is what keeps
                # the id from being mistaken for a TPDB one.
                tpdb_id=str(mapped["stashdb_id"]),
                tpdb_kind="scene",
                tpdb_title=mapped["title"],
                tpdb_site=mapped["site_name"],
                tpdb_date=aired_at.strftime("%Y-%m-%d") if aired_at else None,
                tpdb_performers=list(mapped["performers"] or []),
                tpdb_poster=mapped["poster_path"],
            )
        )

    match = best_match(candidates)

    if match is not None:
        match.provider = "stashdb"

    return match


#: Which column on a MediaItem or CollectionEntry each provider's id belongs
#: in. Sharing one map is the point: the id routing used to be an if/else at
#: every call site, so adding a provider meant an unknown one fell through to
#: `tpdb_id` -- silently filing an Adult Empire product number as a TPDB uuid,
#: which then poisons every TPDB lookup and dedupe with no way to tell where
#: the value came from.
PROVIDER_ID_ATTRIBUTE = {
    "tpdb": "tpdb_id",
    "stashdb": "stashdb_id",
    "adultempire": "adultempire_id",
}


def assign_provider_id(target: object, match: Match) -> bool:
    """Store `match.tpdb_id` on `target` in the column its provider owns.

    Returns False, having written nothing, when the provider is unknown or
    the target has no such column. Refusing is deliberate: writing the id to
    the wrong column is worse than not recording it, because it is
    indistinguishable afterwards from a genuine id of that kind.
    """

    attribute = PROVIDER_ID_ATTRIBUTE.get(match.provider)

    if attribute is None:
        logger.warning(
            f"Not storing id {match.tpdb_id!r}: provider {match.provider!r} "
            "has no column. Add it to PROVIDER_ID_ATTRIBUTE."
        )
        return False

    if not hasattr(target, attribute):
        logger.warning(
            f"Not storing {match.provider} id: {type(target).__name__} has no "
            f"{attribute!r} column."
        )
        return False

    setattr(target, attribute, match.tpdb_id)

    return True


def resolve_movie(
    *,
    title: str,
    studio: str | None = None,
    year: int | None = None,
    performers: list[str] | None = None,
    year_offset: int = 0,
) -> Match | None:
    """The best acceptable match from the highest-priority provider that has one.

    The returned `Match` carries `provider`, which callers MUST consult before
    storing `tpdb_id` -- for StashDB that value is a StashDB UUID and belongs
    in `stashdb_id`.
    """

    kwargs = dict(
        title=title,
        studio=studio,
        year=year,
        performers=performers,
        year_offset=year_offset,
    )

    attempted = list[str]()

    for provider in _provider_order():
        try:
            if provider == "tpdb":
                if not _tpdb_enabled():
                    continue

                attempted.append(provider)
                match = _resolve_via_tpdb(**kwargs)
            elif provider == "adultempire":
                if not _adultempire_enabled():
                    continue

                attempted.append(provider)
                match = _resolve_via_adultempire(**kwargs)
            elif provider == "stashdb":
                if not di[StashdbApi].configured:
                    continue

                attempted.append(provider)
                match = _resolve_via_stashdb(**kwargs)
            else:
                continue
        except StashdbApiError as e:
            # Expected and survivable: a bad key, a rate limit, an outage.
            logger.debug(f"{provider} could not resolve {title!r}: {e}")
            continue
        except Exception as e:
            # Anything else is a bug in a provider, and a bug in one provider
            # must not take the other down with it.
            logger.warning(f"{provider} raised while resolving {title!r}: {e}")
            continue

        if match is not None:
            if provider != attempted[0]:
                # Worth a line: it is the difference between "TPDB had it" and
                # "TPDB did not, and the fallback earned its keep".
                logger.debug(
                    f"Resolved {title!r} from {provider} after "
                    f"{attempted[0]} found no match"
                )

            return match

    if not attempted:
        logger.debug(
            f"No metadata provider is configured; cannot resolve {title!r}. "
            "Set a TPDB token, enable Adult Empire, or add a StashDB key."
        )

    return None
