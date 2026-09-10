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
from program.services.recommendations import tpdb_lookup
from program.settings import settings_manager

# StashDB's search is a single fuzzy `searchScenes` call that already returns
# full records -- there is no flat/detail split to work around as there is on
# TPDB -- so every hit can be scored directly and a wider net costs one
# request rather than one per candidate.
STASHDB_SEARCH_LIMIT = 15


def _provider_order() -> list[str]:
    return list(settings_manager.settings.metadata.providers or ["tpdb"])


def _tpdb_enabled() -> bool:
    settings = settings_manager.settings.tpdb

    return bool(settings.enabled and settings.api_token)


def _resolve_via_tpdb(**kwargs) -> Match | None:
    api = di[TpdbApi]

    return tpdb_lookup.resolve_movie(api, **kwargs)


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
            "Set a TPDB token or a StashDB API key."
        )

    return None
