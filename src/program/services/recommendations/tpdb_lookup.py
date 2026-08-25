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

from program.apis.tpdb_api import TpdbApi
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
