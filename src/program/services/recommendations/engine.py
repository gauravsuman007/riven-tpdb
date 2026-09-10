"""Two recommendation engines, deliberately not one.

    * The **movie engine** ranks the local catalogue -- every
      :class:`CollectionEntry` already mirrored from Adult Empire's shelves and
      the AVN corpus. It is pure local compute over data that has already been
      fetched, so it costs nothing per request and works with no provider
      configured at all.
    * The **scene engine** asks StashDB, which facets server-side. One call
      answers "Outdoors AND Romance" across its whole corpus, which nothing
      local could do.

Sharing one engine would make both worse. StashDB's corpus is modern
amateur/gonzo scenes and is the *wrong* place to look for "something with a
real plot"; the storefront corpus is movie-shaped and has no scene-level tags
at all. Different corpora, different signals, different acceptance bars.

Ranking keeps its provenance. Every result carries the components that
produced its score, so a row can say why a title is in it -- which is the only
thing that makes a bad recommendation fixable rather than merely annoying.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from program.apis.stashdb_api import StashdbApi, StashdbApiError
from program.db.db import db_session
from program.media.collection import Collection, CollectionEntry
from program.media.item import MediaItem
from program.services.recommendations.facets import Facet, vocabulary
from program.services.recommendations.intents import Intent, library
from program.utils.time import utcnow

# --------------------------------------------------------------- weighting

#: Award bodies disagree usefully -- XRCO is critics, AVN is industry, XBIZ is
#: business -- so they are weighted apart rather than pooled into a single
#: "award" number that would hide the disagreement. Only AVN is ingested
#: today; the other two are listed so adding their corpora is a data change.
AWARD_WEIGHTS: dict[str, float] = {
    "avn": 1.0,
    "xrco": 1.2,
    "xbiz": 0.8,
}

#: A nomination is evidence too, just weaker. Not a fifth of a win by any
#: measured fact -- it is a stated editorial choice, which is why it is one
#: constant here rather than scattered through the scoring.
NOMINEE_FRACTION = 0.35

WEIGHTS: dict[str, float] = {
    "awards": 3.0,
    "rating": 2.0,
    "demand": 1.5,
    "recency": 0.5,
    "affinity": 1.5,
    "intent": 4.0,
}

#: Ratings need a prior or one five-star vote outranks four hundred at 4.8.
#: Adult Empire publishes no vote count, so the shrink is toward the corpus
#: mean by a fixed pseudo-count -- weaker than a real Bayesian average and
#: honest about it.
RATING_PRIOR_WEIGHT = 4.0

#: How fast a title's recency contribution decays, in years. Long, because the
#: catalogue's best features are decades old and a short half-life would rank
#: this month's releases over them by arithmetic alone.
RECENCY_HALF_LIFE_YEARS = 12.0


@dataclass(slots=True)
class Recommendation:
    """One ranked title, with the reasons it ranked."""

    key: str
    title: str
    score: float
    kind: str = "movie"
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
    performers: list[str] = field(default_factory=list)
    poster_path: str | None = None
    requested: bool = False
    #: Score components, retained so a result can explain itself.
    signals: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class LibraryTaste:
    """What the library says this person already likes.

    Built from owned items only. A wishlist is an aspiration; what was
    actually requested, kept and watched is evidence.
    """

    performers: dict[str, int] = field(default_factory=dict)
    studios: dict[str, int] = field(default_factory=dict)
    facets: dict[str, int] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (self.performers or self.studios or self.facets)

    def affinity(
        self,
        *,
        performers: Iterable[str] | None,
        studio: str | None,
        facets: Iterable[Facet] | None = None,
    ) -> tuple[float, list[str]]:
        """0.0-1.0 overlap with the library, plus what overlapped."""

        if self.empty:
            return 0.0, []

        reasons: list[str] = []
        score = 0.0

        shared = [
            name
            for name in (performers or [])
            if _fold(name) in self.performers
        ]

        if shared:
            # Saturating rather than linear: three shared performers is a
            # strong signal, ten is not three times stronger.
            score += 0.6 * min(1.0, len(shared) / 3.0)
            reasons.extend(f"cast: {name}" for name in shared[:3])

        if studio and _fold(studio) in self.studios:
            score += 0.3
            reasons.append(f"studio: {studio}")

        overlap = [
            facet.key for facet in (facets or []) if facet.key.lower() in self.facets
        ]

        if overlap:
            score += 0.1 * min(1.0, len(overlap) / 3.0)
            reasons.extend(overlap[:2])

        return min(1.0, score), reasons


def _fold(value: str | None) -> str:
    return (value or "").strip().lower()


def build_taste(session: Any, limit: int = 500) -> LibraryTaste:
    """Read the owned library into a taste profile."""

    taste = LibraryTaste()

    items = (
        session.execute(
            select(MediaItem)
            .where(MediaItem.type == "movie")
            .order_by(MediaItem.requested_at.desc())
            .limit(limit)
        )
        .unique()
        .scalars()
        .all()
    )

    for item in items:
        for performer in item.performers or []:
            key = _fold(performer)

            if key:
                taste.performers[key] = taste.performers.get(key, 0) + 1

        studio = _fold(item.network)

        if studio:
            taste.studios[studio] = taste.studios.get(studio, 0) + 1

        for facet in vocabulary.facets(item.genres):
            key = facet.key.lower()
            taste.facets[key] = taste.facets.get(key, 0) + 1

    return taste


# ------------------------------------------------------------ movie engine


class MovieEngine:
    """Ranks the locally mirrored catalogue.

    Everything it reads is already in the database -- brochure shelves, award
    ballots, imported lists -- so a request costs one query and some
    arithmetic. Nothing here fetches.
    """

    def __init__(self) -> None:
        self.intents = library

    # --- facets on an entry ------------------------------------------------

    @staticmethod
    def entry_facets(entry: CollectionEntry) -> set[Facet]:
        """Everything this entry says about itself, typed.

        A ``CollectionEntry`` carries no tag list, so the facets are derived
        from what it *does* carry. The award category is the strongest of them
        and the least obvious: "Best Parody" and "Best Drama" are genre
        statements made by an editorial body, which is better evidence than a
        storefront's own keywords.
        """

        sources: list[str] = []

        if entry.category:
            # "Best Drama Release" -> the words that carry the genre.
            sources.extend(
                word
                for word in entry.category.replace("-", " ").split()
                if word.lower() not in {"best", "release", "video", "movie", "of", "the", "year"}
            )

        if entry.media_item is not None:
            sources.extend(entry.media_item.genres or [])

        return vocabulary.facets(sources)

    # --- the ranking ------------------------------------------------------

    def rank(
        self,
        *,
        intent: Intent | None = None,
        limit: int = 30,
        sources: Sequence[str] | None = None,
        studio: str | None = None,
        include_requested: bool = False,
    ) -> list[Recommendation]:
        return self.rank_many(
            [intent],
            limit=limit,
            sources=sources,
            studio=studio,
            include_requested=include_requested,
        )[0]

    def rank_many(
        self,
        intents: Sequence[Intent | None],
        *,
        limit: int = 30,
        sources: Sequence[str] | None = None,
        studio: str | None = None,
        include_requested: bool = False,
    ) -> list[list[Recommendation]]:
        """Rank the same corpus against several intents in one read.

        The Explore page asks for half a dozen rails at once and the corpus is
        the whole award ballot plus every mirrored shelf -- tens of thousands
        of rows. Ranking per rail would re-read all of it, and rebuild the
        taste profile and the award index with it, once per row on the page.
        The scoring is cheap; the reading is not.
        """

        with db_session() as session:
            query = (
                select(CollectionEntry)
                .join(Collection, CollectionEntry.collection_id == Collection.id)
                .options(joinedload(CollectionEntry.collection))
            )

            if sources:
                query = query.where(Collection.source.in_(list(sources)))

            if studio:
                query = query.where(CollectionEntry.studio.ilike(f"%{studio}%"))

            if not include_requested:
                query = query.where(CollectionEntry.media_item_id.is_(None))

            entries = session.execute(query).unique().scalars().all()

            if not entries:
                return [[] for _ in intents]

            taste = build_taste(session)
            awards = self._award_index(session)
            mean_rating = self._mean_rating(entries)

            # One title can appear on several shelves and in several award
            # categories. Ranking the rows would fill a rail with the same
            # film; collapse to the best-scoring row per title first.
            best: list[dict[str, Recommendation]] = [{} for _ in intents]

            for entry in entries:
                for slot, intent in enumerate(intents):
                    scored = self._score(
                        entry,
                        intent=intent,
                        taste=taste,
                        awards=awards,
                        mean_rating=mean_rating,
                    )

                    if scored is None:
                        continue

                    existing = best[slot].get(scored.key)

                    if existing is None or scored.score > existing.score:
                        best[slot][scored.key] = scored

            return [
                sorted(slot.values(), key=lambda r: r.score, reverse=True)[:limit]
                for slot in best
            ]

    # --- signals -----------------------------------------------------------

    @staticmethod
    def _mean_rating(entries: Sequence[CollectionEntry]) -> float:
        rated = [entry.rating for entry in entries if entry.rating]

        return sum(rated) / len(rated) if rated else 0.0

    @staticmethod
    def _award_index(session: Any) -> dict[str, tuple[float, list[str]]]:
        """Award weight per title, pooled across bodies and ceremonies.

        Keyed on the folded title because an award corpus and a storefront
        share no id -- that is the whole reason the awards service resolves
        against providers in the first place, and pooling here must not wait
        on that having succeeded.
        """

        index: dict[str, tuple[float, list[str]]] = {}
        rows = session.execute(
            select(
                CollectionEntry.title,
                CollectionEntry.winner,
                CollectionEntry.category,
                Collection.source,
                Collection.year,
            )
            .join(Collection, CollectionEntry.collection_id == Collection.id)
            .where(Collection.source.in_(list(AWARD_WEIGHTS)))
        ).all()

        pooled: dict[str, tuple[float, list[str]]] = defaultdict(lambda: (0.0, []))

        for title, winner, category, source, year in rows:
            key = _fold(title)

            if not key:
                continue

            weight = AWARD_WEIGHTS.get(source, 1.0) * (
                1.0 if winner else NOMINEE_FRACTION
            )
            score, reasons = pooled[key]
            label = f"{source.upper()} {year or ''} {category or ''}".strip()
            pooled[key] = (
                score + weight,
                reasons if len(reasons) >= 3 else [*reasons, label],
            )

        index.update(pooled)

        return index

    def _score(
        self,
        entry: CollectionEntry,
        *,
        intent: Intent | None,
        taste: LibraryTaste,
        awards: dict[str, tuple[float, list[str]]],
        mean_rating: float,
    ) -> Recommendation | None:
        facets = self.entry_facets(entry)
        year = entry.year or (entry.released_at.year if entry.released_at else None)
        reasons: list[str] = []
        signals: dict[str, float] = {}

        if intent is not None:
            verdict = intent.evaluate(
                facets, runtime=entry.duration_minutes, year=year
            )

            if verdict is None:
                # Disqualified, not merely unranked. See Intent.evaluate.
                return None

            intent_score, intent_reasons = verdict
            signals["intent"] = intent_score
            reasons.extend(intent_reasons)

        award_score, award_reasons = awards.get(_fold(entry.title), (0.0, []))

        if award_score:
            # Diminishing: a title with eleven nominations is not eleven times
            # the film one with two is.
            signals["awards"] = min(1.0, math.log1p(award_score) / math.log(6))
            reasons.extend(award_reasons[:2])

        if entry.rating:
            # Shrunk toward the corpus mean, then put on 0-1. Adult Empire
            # rates out of five.
            shrunk = (
                entry.rating + mean_rating * RATING_PRIOR_WEIGHT
            ) / (1.0 + RATING_PRIOR_WEIGHT)
            signals["rating"] = max(0.0, min(1.0, shrunk / 5.0))

        if entry.rank:
            # Rank 1 on a bestseller shelf is demand; rank 140 is barely a
            # statement at all.
            signals["demand"] = max(0.0, 1.0 - (entry.rank - 1) / 100.0)

        if year:
            age = max(0.0, utcnow().year - year)
            signals["recency"] = 0.5 ** (age / RECENCY_HALF_LIFE_YEARS)

        affinity, affinity_reasons = taste.affinity(
            performers=entry.performers, studio=entry.studio, facets=facets
        )

        if affinity:
            signals["affinity"] = affinity
            reasons.extend(affinity_reasons)

        score = sum(WEIGHTS.get(name, 0.0) * value for name, value in signals.items())

        if score <= 0:
            return None

        collection = entry.collection

        return Recommendation(
            key=_fold(entry.title) or f"entry-{entry.id}",
            title=entry.title,
            score=round(score, 4),
            entry_id=entry.id,
            collection_key=collection.key if collection else None,
            external_source=entry.external_source,
            external_id=entry.external_id,
            tpdb_id=entry.tpdb_id,
            stashdb_id=entry.stashdb_id,
            adultempire_id=entry.adultempire_id,
            studio=entry.studio,
            year=year,
            rating=entry.rating,
            duration_minutes=entry.duration_minutes,
            performers=list(entry.performers or []),
            poster_path=entry.poster_path,
            requested=entry.requested,
            signals={name: round(value, 4) for name, value in signals.items()},
            reasons=_dedupe(reasons)[:5],
        )


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []

    for value in values:
        key = _fold(value)

        if key and key not in seen:
            seen.add(key)
            out.append(value)

    return out


# ------------------------------------------------------------ scene engine

_SCENE_FIELDS = """
    id
    title
    release_date
    duration
    studio { id name }
    performers { performer { id name } }
    tags { id name }
    images { id url width height }
"""

_QUERY_SCENES = f"""
query Scenes($input: SceneQueryInput!) {{
    queryScenes(input: $input) {{
        count
        scenes {{
            {_SCENE_FIELDS}
        }}
    }}
}}
"""


class SceneEngine:
    """Intent-shaped queries against StashDB, faceted server-side."""

    def __init__(self, api: StashdbApi | None = None) -> None:
        self.api = api or StashdbApi()

    @property
    def available(self) -> bool:
        """Whether this engine can answer at all.

        Two conditions, and both are reported separately by the router: a key,
        and an ingested tag graph. Without the graph there are no tag ids, so
        a query would silently degrade into "newest scenes" wearing an
        intent's name.
        """

        return self.api.configured and not vocabulary.graph.empty

    def rank(self, intent: Intent, limit: int = 30) -> list[Recommendation]:
        if not self.available:
            return []

        included, excluded = intent.stashdb_tag_ids()

        if not included:
            logger.debug(
                f"Intent {intent.name!r} names no tag StashDB knows; "
                "not querying the scene engine."
            )

            return []

        payload: dict[str, Any] = {
            "page": 1,
            "per_page": min(limit * 2, 100),
            "sort": "DATE",
            "direction": "DESC",
            "tags": {
                "value": included,
                # ANY, not ALL: an intent's `any` list is a pull, and
                # INCLUDES_ALL over eleven location tags would demand a scene
                # shot on a beach *and* a boat *and* a balcony, which returns
                # nothing.
                "modifier": "INCLUDES",
            },
        }

        if excluded:
            payload["tags"]["excludes"] = excluded

        try:
            data = self.api._query(_QUERY_SCENES, {"input": payload})  # noqa: SLF001
        except StashdbApiError as e:
            logger.warning(f"StashDB scene query failed for {intent.name!r}: {e}")

            return []

        scenes = (data.get("queryScenes") or {}).get("scenes") or []
        ranked: list[Recommendation] = []

        for scene in scenes:
            recommendation = self._score(scene, intent)

            if recommendation is not None:
                ranked.append(recommendation)

        ranked.sort(key=lambda r: r.score, reverse=True)

        return ranked[:limit]

    def _score(self, scene: dict[str, Any], intent: Intent) -> Recommendation | None:
        tags = [str((tag or {}).get("name") or "") for tag in scene.get("tags") or []]
        facets = vocabulary.facets(tags)
        duration = scene.get("duration")
        minutes = int(duration // 60) if isinstance(duration, (int, float)) else None
        release = str(scene.get("release_date") or "")
        year = int(release[:4]) if release[:4].isdigit() else None

        verdict = intent.evaluate(facets, runtime=minutes, year=year)

        if verdict is None:
            # StashDB's INCLUDES filter cannot express the whole intent -- a
            # `none` term with no tag id is invisible to it -- so the local
            # evaluation still gets the last word.
            return None

        intent_score, reasons = verdict
        signals = {"intent": intent_score}

        if year:
            age = max(0.0, utcnow().year - year)
            signals["recency"] = 0.5 ** (age / RECENCY_HALF_LIFE_YEARS)

        images = sorted(
            (image for image in scene.get("images") or [] if image.get("url")),
            key=lambda image: image.get("width") or 0,
            reverse=True,
        )
        studio = (scene.get("studio") or {}).get("name")

        return Recommendation(
            key=f"stashdb-{scene.get('id')}",
            title=str(scene.get("title") or "Untitled"),
            kind="scene",
            score=round(
                sum(WEIGHTS.get(name, 0.0) * value for name, value in signals.items()), 4
            ),
            stashdb_id=str(scene.get("id") or "") or None,
            studio=studio,
            year=year,
            duration_minutes=minutes,
            performers=[
                str(((credit or {}).get("performer") or {}).get("name") or "")
                for credit in scene.get("performers") or []
            ],
            poster_path=images[0]["url"] if images else None,
            signals={name: round(value, 4) for name, value in signals.items()},
            reasons=_dedupe(reasons)[:5],
        )


@dataclass(slots=True)
class StudioSplit:
    """A studio's catalogue seen twice: by critics and by the till."""

    studio: str
    baseline_rating: float | None
    deep_cuts: list[Recommendation]
    popular: list[Recommendation]


class StudioEngine:
    """"Best of this studio", where "best" is not "best-selling".

    Adult Empire carries a rating per title but will not order by it -- which
    is why ``STUDIO_SORTS`` is ``("bestseller", "trending")`` and why a studio
    page shows demand and calls it quality. The fix is local: once a studio's
    catalogue is mirrored, rank it here.

    The split is the point. High rating with poor sales rank is a deep cut;
    strong sales with an ordinary rating is a popular title. Those are two
    different answers to two different questions, so they are returned as two
    rows and labelled honestly rather than blended into one list that answers
    neither.
    """

    #: How far above its own studio's mean a title must rate to count as a
    #: deep cut. Deviation from the studio baseline, not from the catalogue's:
    #: a house that rates 4.6 across the board says nothing by rating 4.6.
    DEVIATION = 0.15

    def __init__(self, engine: MovieEngine | None = None) -> None:
        self.movies = engine or MovieEngine()

    def split(self, studio: str, limit: int = 12) -> "StudioSplit":
        ranked = self.movies.rank(
            studio=studio, limit=200, include_requested=True
        )
        rated = [r for r in ranked if r.rating is not None]

        if not rated:
            return StudioSplit(studio=studio, baseline_rating=None, deep_cuts=[], popular=ranked[:limit])

        baseline = sum(r.rating or 0.0 for r in rated) / len(rated)
        deep_cuts = [
            r
            for r in rated
            if (r.rating or 0.0) >= baseline + self.DEVIATION
            and (r.signals.get("demand", 0.0) < 0.5)
        ]
        popular = sorted(
            ranked, key=lambda r: r.signals.get("demand", 0.0), reverse=True
        )

        deep_cuts.sort(key=lambda r: (r.rating or 0.0, r.score), reverse=True)

        return StudioSplit(
            studio=studio,
            baseline_rating=round(baseline, 3),
            deep_cuts=deep_cuts[:limit],
            popular=[r for r in popular if r.signals.get("demand")][:limit],
        )


movies = MovieEngine()
scenes = SceneEngine()
studios = StudioEngine(movies)
