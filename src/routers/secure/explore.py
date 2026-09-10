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

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from program.services.recommendations.engine import (
    Recommendation,
    movies,
    scenes,
    studios,
)
from program.services.recommendations.facets import vocabulary
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

    # The unfiltered rail first: it is the one that always has something in
    # it, because it needs neither an ingested vocabulary nor a provider.
    baseline = movies.rank(limit=per_rail)

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

    for intent in library.for_engine("movies"):
        items = movies.rank(intent=intent, limit=per_rail)

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
