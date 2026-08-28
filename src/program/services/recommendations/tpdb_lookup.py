"""Finding the TPDB record for a title we already have metadata for.

This is the shape every "attach TPDB to something that arrived from elsewhere"
path needs, and it is subtle enough that having two copies of it would be a
liability. Two passes, always:

    1. ``/movies?q=`` returns a *flat* record -- no nested ``site``, no
       ``performers``, only a top-level ``site_id``. Shortlisting is the only
       thing it is good for, because title similarity is the only signal it
       carries.
    2. ``/movies/{id}`` returns the full shape. Scoring happens here, against
       studio, cast and date, which is what the acceptance bar is calibrated
       for.

Scoring the flat records directly is the trap: studio and cast stay unset, the
score never clears ``ACCEPT_SCORE``, and nothing ever matches -- silently, with
no error to notice.
"""

from datetime import datetime

from program.utils.time import utcnow

from kink import di
from loguru import logger

from program.apis.tpdb_api import TpdbApi, TpdbApiError
from program.media.collection import MATCH_MATCHED, CollectionEntry
from program.settings import settings_manager
from program.services.awards.matching import (
    MIN_TITLE_RATIO,
    best_match,
    evaluate_candidate,
    title_ratio,
)

# Enough to cover a title TPDB lists under several editions, without spending a
# detail request on every search hit.
DETAIL_CANDIDATES = 3


def resolve_movie(
    api: TpdbApi,
    *,
    title: str,
    studio: str | None = None,
    year: int | None = None,
    performers: list[str] | None = None,
    year_offset: int = 0,
):
    """The best acceptable TPDB movie for this title, or None.

    ``year_offset`` is subtracted from ``year`` before comparing: an award
    ceremony year is one *after* the release, while a storefront year is the
    release year already. Getting this wrong costs a match on every title.
    """

    results = api.search_movies_text(title, per_page=20) or []

    shortlist = sorted(
        (
            (title_ratio(title, result.title or ""), result)
            for result in results
            if result.id and title_ratio(title, result.title or "") >= MIN_TITLE_RATIO
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )[:DETAIL_CANDIDATES]

    candidates = []

    for _ratio, result in shortlist:
        detail = api.get_movie(result.id)

        if detail is None:
            # Skipped rather than scored off the flat record: a flat record
            # cannot supply site or cast, so scoring it would produce a
            # confident-looking title-only match.
            continue

        candidates.append(
            evaluate_candidate(
                entry_title=title,
                entry_studio=studio,
                entry_year=year,
                year_offset=year_offset,
                entry_performers=list(performers or []),
                tpdb_id=detail.id or result.id,
                tpdb_kind="movie",
                tpdb_title=detail.title,
                tpdb_site=detail.site.name if detail.site else None,
                tpdb_date=detail.date,
                tpdb_performers=[p.name for p in detail.performers if p.name],
                tpdb_poster=detail.poster
                or (detail.posters.large if detail.posters else None),
            )
        )

    return best_match(candidates)


def client() -> TpdbApi | None:
    """The TPDB client, or None when the fork is running without a token."""

    if not settings_manager.settings.tpdb.api_token:
        return None

    try:
        return di[TpdbApi]
    except Exception as exc:  # pragma: no cover - DI misconfiguration
        logger.debug(f"TPDB client unavailable: {exc}")
        return None


def enrich_entry(entry: CollectionEntry) -> bool:
    """Attach TPDB metadata to a catalogue entry that arrived without it.

    Best effort by design, and that is load-bearing rather than lazy: measured
    against Adult Empire's all-time bestsellers, TPDB has a confident match for
    roughly four titles in five. The fifth is usually a one-word title like
    "Nurses" or a 1979 release, where the matcher correctly refuses to guess.
    Those titles are still perfectly downloadable from the storefront's own
    metadata -- studio, year and cast is exactly what the scrapers match on --
    so a miss must leave the entry usable rather than reject it.

    Mutates ``entry``; the caller owns the commit.
    """

    if entry.tpdb_id or not entry.title:
        return False

    api = client()

    if api is None:
        return False

    try:
        match = resolve_movie(
            api,
            title=entry.title,
            studio=entry.studio,
            year=entry.year,
            performers=list(entry.performers or []),
            # A storefront year is the release year; only a ceremony year is
            # one ahead of it.
            year_offset=0,
        )
    except TpdbApiError as exc:
        logger.debug(f"TPDB unavailable while resolving {entry.title!r}: {exc}")
        return False
    except Exception as exc:
        logger.debug(f"TPDB lookup failed for {entry.title!r}: {exc}")
        return False

    if match is None:
        logger.debug(f"No TPDB match for {entry.title!r}; using storefront metadata")
        return False

    entry.tpdb_id = match.tpdb_id
    entry.tpdb_kind = match.kind
    entry.match_score = match.score
    entry.match_state = MATCH_MATCHED
    entry.matched_at = utcnow()

    if match.poster:
        entry.poster_path = match.poster

    logger.debug(f"Matched {entry.title!r} to TPDB {match.tpdb_id}")

    return True
