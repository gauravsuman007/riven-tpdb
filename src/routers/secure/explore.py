"""The Explore surface: recommendations, awards and the brochure in one place.

The page this serves is a hub, so the default endpoint is ``/explore/rows`` --
every rail in one request. The alternative, a request per rail, is what made
the discovery page slow enough to notice: rows render together or the page
reflows under the reader.

Each rail carries its own ``reason`` and every title carries the signals that
ranked it. That is not decoration. A recommendation nobody can interrogate is
a recommendation nobody can correct, and the whole engine is built on
inspectable expressions precisely so it can be argued with.
"""

from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel

from program.services.recommendations.engine import (
    Recommendation,
    movies,
    scenes,
    studios,
)
from program.services.recommendations.adultempire_categories import (
    DEFAULT_CATEGORIES,
    category_index,
)
from program.services.recommendations.facets import vocabulary
from program.services.recommendations.ratings import rating_backfill
from program.services.recommendations.intents import library

router = APIRouter(prefix="/explore", tags=["explore"])


class RecommendationResponse(BaseModel):
    """One ranked title.

    ``entry_id`` is what the request path needs -- a rail item is a
    ``CollectionEntry``, exactly like a brochure or award row, so requesting it
    reuses ``POST /collections/entries/{id}/request`` rather than inventing a
    second way in. Scene results have no entry and therefore no request path
    yet; they are a browsing surface until one exists.
    """

    key: str
    title: str
    kind: str
    score: float
    entry_id: int | None = None
    collection_key: str | None = None
    external_source: str | None = None
    external_id: str | None = None
    tpdb_id: str | None = None
    stashdb_id: str | None = None
    adultempire_id: str | None = None
    studio: str | None = None
    year: int | None = None
    rating: float | None = None
    duration_minutes: int | None = None
    performers: list[str] = []
    poster_path: str | None = None
    requested: bool = False
    signals: dict[str, float] = {}
    reasons: list[str] = []


class IntentResponse(BaseModel):
    name: str
    label: str
    description: str
    engines: list[str]
    #: How many of this intent's terms StashDB can actually filter on. Zero
    #: means the scene engine cannot serve it, which is a fact about the tag
    #: graph rather than a fault, and the page says so instead of showing an
    #: empty row.
    resolvable_tags: int


class Rail(BaseModel):
    """One row on the Explore page."""

    key: str
    title: str
    reason: str
    kind: Literal["movies", "scenes"]
    items: list[RecommendationResponse]


class ExploreRows(BaseModel):
    rails: list[Rail]
    #: Why a rail is missing, when one is. Reported rather than silently
    #: omitted: "the scene engine needs a StashDB key" is actionable, an
    #: absent row is not.
    notices: list[str]


class CategoryIndexStatus(BaseModel):
    """The movie corpus's genre index, and whether it has been built."""

    built: bool
    running: bool
    titles: int
    categories: list[str]
    fetched_at: float | None


class VocabularyStatus(BaseModel):
    ingested: bool
    tags: int
    categories: dict[str, int]
    scene_engine_available: bool


def _response(recommendation: Recommendation) -> RecommendationResponse:
    return RecommendationResponse(**asdict(recommendation))


@router.get("/intents", operation_id="list_intents")
def list_intents() -> list[IntentResponse]:
    """Every named intent, with whether StashDB can answer it."""

    responses: list[IntentResponse] = []

    for intent in library.intents.values():
        included, _ = intent.stashdb_tag_ids()
        responses.append(
            IntentResponse(
                name=intent.name,
                label=intent.label,
                description=intent.description,
                engines=list(intent.engines),
                resolvable_tags=len(included),
            )
        )

    return sorted(responses, key=lambda i: i.label)


@router.get("/vocabulary", operation_id="get_facet_vocabulary")
def vocabulary_status() -> VocabularyStatus:
    graph = vocabulary.graph

    return VocabularyStatus(
        ingested=not graph.empty,
        tags=len(graph.ids),
        categories=graph.categories(),
        scene_engine_available=scenes.available,
    )


@router.post("/vocabulary/ingest", operation_id="ingest_facet_vocabulary")
def ingest_vocabulary(force: Annotated[bool, Query()] = False) -> VocabularyStatus:
    """Read StashDB's tag graph. ~30 calls; the graph barely moves."""

    vocabulary.ingest(force=force)

    return vocabulary_status()


@router.get("/categories", operation_id="get_category_index")
def category_status() -> CategoryIndexStatus:
    return CategoryIndexStatus(
        built=not category_index.empty,
        running=category_index.running,
        titles=len(category_index.index),
        categories=list(DEFAULT_CATEGORIES),
        fetched_at=category_index.fetched_at or None,
    )


@router.post("/categories/sync", operation_id="sync_category_index")
def sync_categories(
    background: BackgroundTasks,
    pages_per_category: Annotated[int, Query(ge=1, le=100)] = 20,
) -> CategoryIndexStatus:
    """Index Adult Empire's categories so movie entries gain genres.

    A product page carries length, year, studio and cast and **no genre at
    all**, so this reads the other direction: the categories an intent asks
    about, and which titles are listed under them. One request per page at the
    storefront's one-per-second courtesy delay, so a default run is a few
    minutes. It therefore runs in the background and this returns immediately
    with the *current* status -- holding an HTTP response open for four
    minutes would time out at the proxy long before the crawl finished, and
    the caller would be told it failed while it was still working. Poll
    ``GET /explore/categories`` for progress.
    """

    background.add_task(category_index.sync, pages_per_category=pages_per_category)

    return category_status()


class RatingBackfillStatus(BaseModel):
    """How far the audience-rating backfill has got.

    Every field is here because the run is minutes long and a progress bar
    that only says "working" is not one. ``pending`` is what is left to do,
    and is what tells a caller whether pressing the button would do anything.
    """

    running: bool
    pending: int
    considered: int
    fetched: int
    rated: int
    failed: int
    started_at: float | None = None
    finished_at: float | None = None
    last_title: str | None = None


def _rating_status() -> RatingBackfillStatus:
    snapshot = rating_backfill.progress.snapshot()

    return RatingBackfillStatus(pending=rating_backfill.pending(), **snapshot)


@router.get("/ratings", operation_id="get_rating_backfill")
def rating_status() -> RatingBackfillStatus:
    return _rating_status()


@router.post("/ratings/sync", operation_id="sync_ratings")
def sync_ratings(
    background: BackgroundTasks,
    limit: Annotated[int, Query(ge=0, le=5000)] = 0,
) -> RatingBackfillStatus:
    """Read the audience rating for every catalogue entry that lacks one.

    A storefront *listing* carries no rating -- 48 of 48 bestseller rows came
    back without one -- but a *product page* does, and the product URL's slug
    is ignored, so ``/{id}/`` is enough. That makes this one request per title
    at the one-per-second courtesy delay: minutes, in the background, with
    progress committed in batches. Poll ``GET /explore/ratings``.
    """

    background.add_task(rating_backfill.sync, limit=limit)

    return _rating_status()


@router.get("/recommendations", operation_id="get_recommendations")
def recommendations(
    intent: Annotated[str | None, Query()] = None,
    engine: Annotated[Literal["movies", "scenes"], Query()] = "movies",
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> list[RecommendationResponse]:
    """One rail's worth of titles for a named intent."""

    resolved = None

    if intent:
        resolved = library.get(intent)

        if resolved is None:
            raise HTTPException(404, f"No intent named {intent!r}")

    if engine == "scenes":
        if resolved is None:
            raise HTTPException(400, "The scene engine needs an intent to query on")

        return [_response(item) for item in scenes.rank(resolved, limit=limit)]

    return [_response(item) for item in movies.rank(intent=resolved, limit=limit)]


@router.get("/rows", operation_id="get_explore_rows")
def rows(
    per_rail: Annotated[int, Query(ge=1, le=50)] = 20,
    include_scenes: Annotated[bool, Query()] = True,
) -> ExploreRows:
    """Every recommendation rail in one request."""

    rails: list[Rail] = []
    notices: list[str] = []

    # One read of the corpus for every movie rail. The award ballot alone runs
    # to five figures of rows, and ranking per rail would re-read all of it --
    # plus rebuild the taste profile and the award index -- once per row on
    # the page.
    movie_intents = library.for_engine("movies")
    ranked = movies.rank_many([None, *movie_intents], limit=per_rail)
    baseline, per_intent = ranked[0], ranked[1:]

    if baseline:
        rails.append(
            Rail(
                key="for-you",
                title="Recommended for you",
                reason=(
                    "Ranked from awards, storefront ratings and what your "
                    "library already contains."
                ),
                kind="movies",
                items=[_response(item) for item in baseline],
            )
        )
    else:
        notices.append(
            "Nothing to rank yet. Enable the Adult Empire brochure or the AVN "
            "corpus in Settings and let one sync finish."
        )

    for intent, items in zip(movie_intents, per_intent):
        if not items:
            continue

        rails.append(
            Rail(
                key=f"movies-{intent.name}",
                title=intent.label,
                reason=intent.description,
                kind="movies",
                items=[_response(item) for item in items],
            )
        )

    if include_scenes:
        if not scenes.available:
            notices.append(
                "Scene recommendations need a StashDB API key and an ingested "
                "tag graph (Explore -> refresh vocabulary)."
            )
        else:
            for intent in library.for_engine("scenes"):
                items = scenes.rank(intent, limit=per_rail)

                if not items:
                    continue

                rails.append(
                    Rail(
                        key=f"scenes-{intent.name}",
                        title=f"{intent.label} (scenes)",
                        reason=intent.description,
                        kind="scenes",
                        items=[_response(item) for item in items],
                    )
                )

    return ExploreRows(rails=rails, notices=notices)


class StudioSplitResponse(BaseModel):
    studio: str
    baseline_rating: float | None
    deep_cuts: list[RecommendationResponse]
    popular: list[RecommendationResponse]


@router.get("/studios/{studio}", operation_id="get_studio_split")
def studio_split(
    studio: str, limit: Annotated[int, Query(ge=1, le=50)] = 12
) -> StudioSplitResponse:
    """A studio seen twice: what rates well, and what sells.

    The storefront can only order by sales, so "best of" there means
    "best-selling". This re-ranks the mirrored catalogue locally and keeps the
    two answers apart instead of blending them into one that answers neither.
    """

    split = studios.split(studio, limit=limit)

    return StudioSplitResponse(
        studio=split.studio,
        baseline_rating=split.baseline_rating,
        deep_cuts=[_response(item) for item in split.deep_cuts],
        popular=[_response(item) for item in split.popular],
    )
