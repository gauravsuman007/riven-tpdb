"""What TPDB does not have for a title: billing order, wide art, IMDb id.

TPDB lists a cast women A-Z then men A-Z, and a TPDB *movie*'s images are
all box covers -- tall. TMDB carries adult features (``include_adult``), with
the cast in credit order and 16:9 backdrops, and links IMDb. Measured on the
library: 40 of 48 TPDB movies match, all 40 with billing order, 31 with a
backdrop, 18 with an IMDb id. Scenes need none of it: a TPDB scene's
``background`` is already the 16:9 still.

Matching is deliberately strict -- exact title (letters and digits only),
release year within one, adult results only -- and a candidate whose cast
shares nobody with TPDB's is refused. Common titles need the year as a SEARCH
FILTER, not just a check: "Drive" (2019) and "Pirates" (2005) are buried under
mainstream films otherwise and never appear in the first page.

Results (misses included) are cached in the TPDB cache under ``extras/<uuid>``,
so a library title's answer is pinned with the rest of its TPDB data and the
size limit covers everything.
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import requests
from loguru import logger

from program.apis.tmdb_api import TMDB_READ_ACCESS_TOKEN

if TYPE_CHECKING:
    from program.apis.tpdb_api import TpdbApi

TMDB = "https://api.themoviedb.org/3"
TMDB_IMAGES = "https://image.tmdb.org/t/p/original"

_background = ThreadPoolExecutor(max_workers=1, thread_name_prefix="title-extras")
_queued: set[str] = set()
_queued_lock = threading.Lock()


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _tmdb(path: str, **params: Any) -> dict[str, Any]:
    response = requests.get(
        f"{TMDB}/{path}",
        params=params,
        headers={"Authorization": f"Bearer {TMDB_READ_ACCESS_TOKEN}"},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def _best_backdrop(details: dict[str, Any]) -> str | None:
    """A textless backdrop when TMDB has one, else its default."""

    backdrops = (details.get("images") or {}).get("backdrops") or []
    # No language means no title text burned in -- what a banner wants.
    textless = [b for b in backdrops if not b.get("iso_639_1")]
    pool = textless or backdrops

    if pool:
        best = max(pool, key=lambda b: (b.get("vote_average") or 0, b.get("width") or 0))
        return best.get("file_path")

    return details.get("backdrop_path")


def match_tmdb(title: str, year: int | None, cast: list[str]) -> dict[str, Any] | None:
    """The TMDB movie that is this TPDB movie, with credits and images, or None."""

    if not title or not year:
        return None

    wanted = _norm(title)
    tpdb_cast = {_norm(name) for name in cast if name}

    for search_year in (year, year - 1, year + 1):
        results = _tmdb(
            "search/movie", query=title, include_adult="true", year=search_year
        ).get("results") or []

        for hit in results:
            if not hit.get("adult") or _norm(hit.get("title")) != wanted:
                continue

            details = _tmdb(
                f"movie/{hit['id']}",
                append_to_response="credits,external_ids,images",
                include_image_language="null,en",
            )
            tmdb_cast = {_norm(c.get("name")) for c in (details.get("credits") or {}).get("cast") or []}

            # Same title and year but nobody in common: a different film.
            if tpdb_cast and tmdb_cast and not tpdb_cast & tmdb_cast:
                continue

            return details

    return None


def compute(api: TpdbApi, uuid: str) -> dict[str, Any]:
    """Look the title up and build its extras. Misses are an answer too."""

    result: dict[str, Any] = {
        "kind": None,
        "tmdb_id": None,
        "imdb_id": None,
        "cast_order": [],
        "wide": None,
    }

    record = api.get_movie(uuid)
    kind = "movie"

    if record is None:
        record = api.get_scene(uuid)
        kind = "scene"

    if record is None:
        return result

    data = record.model_dump()

    # The record's own type decides, not which endpoint answered first: a
    # scene must never be matched on TMDB as if it were a film. (TPDB does
    # file some single-performer releases as movies -- Naughty America's
    # "Abella Danger" is one, and TMDB has it too, under that studio.)
    if str(data.get("type") or "").lower() == "scene":
        kind = "scene"
    elif str(data.get("type") or "").lower() == "movie":
        kind = "movie"

    result["kind"] = kind
    background = data.get("background") or {}

    if kind == "scene":
        # A scene's background is its 16:9 still.
        result["wide"] = background.get("full") or background.get("large")
        return result

    cast = [p.get("name") for p in data.get("performers") or [] if isinstance(p, dict)]
    year = None

    if data.get("date"):
        try:
            year = int(str(data["date"])[:4])
        except ValueError:
            year = None

    try:
        details = match_tmdb(data.get("title") or "", year, [c for c in cast if c])
    except requests.RequestException as exc:
        # Not cached: a TMDB outage is not a miss.
        raise RuntimeError(f"TMDB unavailable: {exc}") from exc

    if details:
        result["tmdb_id"] = details.get("id")
        result["imdb_id"] = (details.get("external_ids") or {}).get("imdb_id") or None
        result["cast_order"] = [
            c.get("name")
            for c in sorted(
                (details.get("credits") or {}).get("cast") or [],
                key=lambda c: c.get("order", 999),
            )
            if c.get("name")
        ]
        backdrop = _best_backdrop(details)

        if backdrop:
            result["wide"] = f"{TMDB_IMAGES}{backdrop}"

    if not result["wide"]:
        # The movie's own scenes, when TPDB links them: each has a 16:9 still.
        for scene in data.get("scenes") or []:
            still = (scene.get("background") or {}) if isinstance(scene, dict) else {}
            wide = still.get("full") or still.get("large")

            if wide:
                result["wide"] = wide
                break

    return result


def _key(uuid: str) -> str:
    # Contains the uuid, so a library title's answer lands in the pinned tier.
    return f"extras/{uuid}"


def cached(api: TpdbApi, uuid: str) -> dict[str, Any] | None:
    return api._cache_get(_key(uuid), allow_stale=True)


def extras(api: TpdbApi, uuid: str) -> dict[str, Any]:
    """This title's extras, computing (and caching) them when absent."""

    hit = cached(api, uuid)

    if hit is not None:
        return hit

    result = compute(api, uuid)
    api._cache_put(_key(uuid), result)

    if result.get("imdb_id"):
        from program.apis import imdb_ratings

        imdb_ratings.want(api, result["imdb_id"])

    return result


def ensure_in_background(api: TpdbApi, uuids: list[str]) -> None:
    """Compute missing extras off the request path, one title at a time."""

    for uuid in uuids:
        with _queued_lock:
            if uuid in _queued:
                continue

            _queued.add(uuid)

        def run(uuid: str = uuid) -> None:
            try:
                extras(api, uuid)
            except Exception as exc:  # noqa: BLE001 -- retried on the next ask
                logger.debug(f"Title extras for {uuid} failed: {exc}")
            finally:
                with _queued_lock:
                    _queued.discard(uuid)

        _background.submit(run)
