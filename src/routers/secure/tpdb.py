"""TPDB discovery endpoints.

Exposes the TPDB catalogue over HTTP so a frontend can browse and recommend
adult content without falling back to TMDB/Trakt.

A note on what TPDB actually provides, because it shapes this module:

    * There is no popularity, view-count or trending signal, and ``rating`` is
      ``0`` on every record sampled from the live API. ``order_by``/``sort`` are
      accepted but silently ignored -- a bogus value returns the same page as a
      valid one.
    * Scenes cannot be filtered by tag. ``tag``/``tags``/``tags[]``/
      ``filter[tags]`` are ignored or return nothing; ``q`` is the only
      parameter that genuinely narrows a scene listing.

So there is nothing to build a faithful "trending" or "top rated" feed on. The
endpoints below are therefore split into two groups:

    * Catalogue passthroughs (``/scenes``, ``/movies``, ``/search``, ``/sites``,
      ``/performers``, ``/tags``) which return TPDB data as-is, newest first.
    * Explicitly *derived* feeds (``/tags/popular``, ``/recommendations``) which
      are computed here from a sample of the live catalogue and the local
      library. They are named as derived rather than dressed up as TPDB
      rankings.
"""

from collections import Counter
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query
from kink import di
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import select

from program.apis.tpdb_api import (
    TpdbApiError,
    TpdbApi,
    TpdbMovie,
    TpdbPerformer,
    TpdbScene,
    TpdbSite,
    TpdbTag,
)
from program.db.db import db_session
from program.media.item import MediaItem
from program.settings import settings_manager

router = APIRouter(prefix="/tpdb", tags=["tpdb"])

# Scene pages are cheap but rate limited; keep sampling bounded.
MAX_SAMPLE_PAGES = 5
DEFAULT_PER_PAGE = 20


class TagCount(BaseModel):
    name: str
    count: int


class PopularTagsResponse(BaseModel):
    """Tag frequency across a sample of the newest scenes.

    Derived locally: TPDB exposes no tag popularity of its own.
    """

    derived_from: str
    scenes_sampled: int
    tags: list[TagCount]


class RecommendationsResponse(BaseModel):
    """Scenes matched against signals taken from the local library."""

    basis: Literal["library", "subscriptions", "latest"]
    matched_sites: list[str]
    matched_performers: list[str]
    items: list[TpdbScene]


def _api() -> TpdbApi:
    if not settings_manager.settings.tpdb.enabled:
        raise HTTPException(status_code=409, detail="TPDB integration is disabled")

    return di[TpdbApi]


def _call[T](fn, *args, **kwargs) -> T:
    """Run a client call, mapping upstream failures onto 502."""

    try:
        return fn(*args, **kwargs)
    except TpdbApiError as exc:
        # A 404 upstream means the record does not exist, which is a client
        # error here; anything else is TPDB failing us and stays a 502.
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail="Not found in TPDB") from exc

        logger.error(f"TPDB request failed: {exc}")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/scenes", operation_id="tpdb_list_scenes")
def list_scenes(
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = DEFAULT_PER_PAGE,
    site_id: Annotated[str | None, Query(description="Site UUID or numeric id")] = None,
) -> list[TpdbScene]:
    """Newest scenes, optionally restricted to a single site."""

    return _call(_api().list_scenes, site_id=site_id, page=page, per_page=per_page)


@router.get("/movies", operation_id="tpdb_list_movies")
def list_movies(
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = DEFAULT_PER_PAGE,
    site_id: Annotated[str | None, Query(description="Site UUID or numeric id")] = None,
) -> list[TpdbMovie]:
    """Newest movies, optionally restricted to a single site."""

    return _call(_api().list_movies, site_id=site_id, page=page, per_page=per_page)


@router.get("/search", operation_id="tpdb_search")
def search(
    query: Annotated[str, Query(min_length=1)],
    type: Literal["scenes", "movies", "performers", "sites"] = "scenes",
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = DEFAULT_PER_PAGE,
) -> list[Any]:
    """Full-text search across one TPDB collection."""

    api = _api()

    match type:
        case "scenes":
            return _call(api.search_scenes_text, query, page=page, per_page=per_page)
        case "movies":
            return _call(api.search_movies_text, query, page=page, per_page=per_page)
        case "performers":
            return _call(api.search_performers, query)
        case "sites":
            return _call(api.search_sites, query)


@router.get("/tags", operation_id="tpdb_list_tags")
def list_tags(
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 100,
) -> list[TpdbTag]:
    """The TPDB tag vocabulary (~2.6k entries)."""

    return _call(_api().list_tags, page=page, per_page=per_page)


@router.get("/tags/popular", operation_id="tpdb_popular_tags")
def popular_tags(
    pages: Annotated[int, Query(ge=1, le=MAX_SAMPLE_PAGES)] = 3,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> PopularTagsResponse:
    """Most frequent tags across the newest scenes.

    Derived locally by counting tags over a sample of recent scenes. TPDB has no
    tag popularity endpoint, so this reflects what is being published now rather
    than what is most watched.
    """

    api = _api()
    counter: Counter[str] = Counter()
    sampled = 0

    for page in range(1, pages + 1):
        scenes = _call(api.list_scenes, page=page, per_page=100)

        if not scenes:
            break

        sampled += len(scenes)

        for scene in scenes:
            for tag in scene.tags:
                if tag.name:
                    counter[tag.name] += 1

    return PopularTagsResponse(
        derived_from="tag frequency across the newest scenes",
        scenes_sampled=sampled,
        tags=[TagCount(name=name, count=count) for name, count in counter.most_common(limit)],
    )


def _library_signals() -> tuple[list[str], list[str], set[str]]:
    """Collect site ids, performer names and known TPDB ids from the library."""

    sites: Counter[str] = Counter()
    performers: Counter[str] = Counter()
    known: set[str] = set()

    with db_session() as session:
        rows = session.execute(
            select(MediaItem.tpdb_id, MediaItem.site_id, MediaItem.performers).where(
                MediaItem.tpdb_id.is_not(None)
            )
        ).all()

    for tpdb_id, site_id, item_performers in rows:
        if tpdb_id:
            known.add(str(tpdb_id))
        if site_id:
            sites[str(site_id)] += 1
        for performer in item_performers or []:
            if performer:
                performers[str(performer).lower()] += 1

    return (
        [site for site, _ in sites.most_common(5)],
        [performer for performer, _ in performers.most_common(20)],
        known,
    )


@router.get("/recommendations", operation_id="tpdb_recommendations")
def recommendations(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    pages: Annotated[int, Query(ge=1, le=MAX_SAMPLE_PAGES)] = 3,
) -> RecommendationsResponse:
    """Recent scenes ranked against what is already in the library.

    Derived locally. Signals are the sites and performers that appear most in
    the library; candidates are recent scenes from those sites, scored by
    performer and site overlap, with anything already in the library removed.

    Falls back to the configured site subscriptions when the library holds no
    TPDB items, and to the plain newest-scenes feed when neither is available.
    """

    api = _api()
    sites, performers, known = _library_signals()
    basis: Literal["library", "subscriptions", "latest"] = "library"

    if not sites:
        configured = [str(site) for site in settings_manager.settings.content.tpdb.sites]

        if configured:
            sites, basis = configured[:5], "subscriptions"
        else:
            basis = "latest"

    candidates: list[TpdbScene] = []

    if sites:
        for site in sites:
            candidates.extend(_call(api.list_scenes, site_id=site, per_page=100))
    else:
        for page in range(1, pages + 1):
            batch = _call(api.list_scenes, page=page, per_page=100)

            if not batch:
                break

            candidates.extend(batch)

    performer_set = set(performers)
    scored: list[tuple[int, TpdbScene]] = []
    seen: set[str] = set()

    for scene in candidates:
        if not scene.id or scene.id in known or scene.id in seen:
            continue

        seen.add(scene.id)
        names = {(p.name or "").lower() for p in scene.performers}
        score = len(names & performer_set) * 2

        if scene.site and str(scene.site.id) in sites:
            score += 1

        scored.append((score, scene))

    scored.sort(key=lambda pair: pair[0], reverse=True)

    return RecommendationsResponse(
        basis=basis,
        matched_sites=sites,
        matched_performers=performers[:10],
        items=[scene for _, scene in scored[:limit]],
    )


@router.get("/sites", operation_id="tpdb_search_sites")
def sites(query: Annotated[str | None, Query()] = None) -> list[TpdbSite]:
    """Search sites (studios/networks) by name."""

    return _call(_api().search_sites, query or "")


@router.get("/sites/{site_id}", operation_id="tpdb_get_site")
def get_site(site_id: Annotated[str, Path()]) -> TpdbSite:
    """A single site by UUID or numeric id."""

    site = _call(_api().get_site, site_id)

    if not site:
        raise HTTPException(status_code=404, detail="Site not found")

    return site


@router.get("/performers/{performer_id}", operation_id="tpdb_get_performer")
def get_performer(performer_id: Annotated[str, Path()]) -> TpdbPerformer:
    """A single performer by id."""

    performer = _call(_api().get_performer, performer_id)

    if not performer:
        raise HTTPException(status_code=404, detail="Performer not found")

    return performer


@router.get("/scenes/{scene_id}", operation_id="tpdb_get_scene")
def get_scene(scene_id: Annotated[str, Path()]) -> TpdbScene:
    """A single scene by UUID."""

    scene = _call(_api().get_scene, scene_id)

    if not scene:
        raise HTTPException(status_code=404, detail="Scene not found")

    return scene


@router.get("/movies/{movie_id}", operation_id="tpdb_get_movie")
def get_movie(movie_id: Annotated[str, Path()]) -> TpdbMovie:
    """A single movie by id."""

    movie = _call(_api().get_movie, movie_id)

    if not movie:
        raise HTTPException(status_code=404, detail="Movie not found")

    return movie
