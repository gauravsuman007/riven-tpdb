"""IMDb ratings for the titles TMDB linked to an IMDb id.

From IMDb's own non-commercial dataset, ``title.ratings.tsv.gz`` (~8 MB, all
of IMDb, refreshed daily). It is NEVER loaded whole: 1.5 million rows would be
~150 MB of Python on a host that is already short of memory. Instead the file
is scanned in a stream and only the ids this app has matched are kept, in a
small JSON map. Scraping imdb.com instead is against its terms; this file is
what IMDb publishes for exactly this.

Everything lives under the TPDB cache directory's ``imdb/``, which the cache's
size eviction walks too (``TpdbApi._enforce_size_limit``): an evicted file is
simply fetched or rebuilt on the next refresh.
"""

from __future__ import annotations

import gzip
import json
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import requests
from loguru import logger

if TYPE_CHECKING:
    from program.apis.tpdb_api import TpdbApi

DATASET_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"
MAX_AGE_SECONDS = 24 * 60 * 60

_lock = threading.Lock()
_memo: dict[str, tuple[float, dict]] = {}


def _dir(api: TpdbApi) -> Path | None:
    if api._cache_dir is None:
        return None

    directory = api._cache_dir / "imdb"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _read_json(path: Path) -> dict:
    """A small JSON file, re-read only when it changes on disk."""

    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}

    hit = _memo.get(str(path))

    if hit and hit[0] == mtime:
        return hit[1]

    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        data = {}

    _memo[str(path)] = (mtime, data)
    return data


def _write_json(path: Path, data: dict) -> None:
    partial = path.with_suffix(".tmp")
    partial.write_text(json.dumps(data), "utf-8")
    partial.replace(path)


def want(api: TpdbApi, tconst: str) -> None:
    """Remember an IMDb id to pick up on the next refresh."""

    directory = _dir(api)

    if directory is None or not tconst:
        return

    with _lock:
        path = directory / "wanted.json"
        wanted = dict(_read_json(path))

        if tconst not in wanted:
            wanted[tconst] = True
            _write_json(path, wanted)


def get(api: TpdbApi, tconst: str | None) -> tuple[float, int] | None:
    """(rating, votes) for an IMDb id, or None when IMDb has none or not yet."""

    directory = _dir(api)

    if directory is None or not tconst:
        return None

    entry = _read_json(directory / "ratings.json").get(tconst)

    return (entry[0], entry[1]) if entry else None


def refresh(api: TpdbApi) -> int:
    """Re-download the dataset when stale and rebuild the map when needed.

    Cheap when nothing changed: two stats and a JSON read. Returns how many
    ids now have a rating.
    """

    directory = _dir(api)

    if directory is None:
        return 0

    dataset = directory / "title.ratings.tsv.gz"
    ratings_path = directory / "ratings.json"

    try:
        stale = time.time() - dataset.stat().st_mtime > MAX_AGE_SECONDS
    except OSError:
        stale = True

    if stale:
        try:
            with requests.get(DATASET_URL, stream=True, timeout=60) as response:
                response.raise_for_status()
                partial = dataset.with_suffix(".part")

                with partial.open("wb") as handle:
                    for chunk in response.iter_content(256 * 1024):
                        handle.write(chunk)

                partial.replace(dataset)
        except (requests.RequestException, OSError) as exc:
            logger.debug(f"IMDb ratings download failed: {exc}")

            if not dataset.exists():
                return 0

    wanted = set(_read_json(directory / "wanted.json"))
    current = _read_json(ratings_path)
    checked = set(current.get("__checked__", []))

    try:
        dataset_newer = dataset.stat().st_mtime > ratings_path.stat().st_mtime
    except OSError:
        dataset_newer = True

    if not wanted or (not dataset_newer and wanted <= checked):
        return len(current) - (1 if "__checked__" in current else 0)

    found: dict = {}

    try:
        with gzip.open(dataset, "rt", encoding="utf-8") as handle:
            next(handle, None)  # header

            for line in handle:
                tconst, _, rest = line.partition("\t")

                if tconst in wanted:
                    rating, _, votes = rest.rstrip("\n").partition("\t")

                    try:
                        found[tconst] = [float(rating), int(votes)]
                    except ValueError:
                        continue
    except (OSError, EOFError) as exc:
        logger.debug(f"IMDb ratings scan failed: {exc}")
        return 0

    found["__checked__"] = sorted(wanted)

    with _lock:
        _write_json(ratings_path, found)

    return len(found) - 1
