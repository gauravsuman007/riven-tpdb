"""StashDB, as a metadata provider.

StashDB is GraphQL, not REST, which changes three things worth knowing before
reading further:

  * **Every query needs the API key, including search.** An unauthenticated
    request comes back HTTP 200 with `{"errors":[{"message":"not
    authorized"}]}` -- not a 401 -- so a missing or wrong key looks exactly
    like a malformed query unless it is checked for by name.
  * **Errors are in the body, not the status.** A GraphQL endpoint answers 200
    for a failed query. Anything reading `response.ok` alone will treat a
    total failure as an empty result.
  * **It is scene-oriented.** Where TPDB models scenes *and* movies, StashDB
    models scenes with a studio attached. Everything Riven needs -- title,
    studio, cast, date, poster -- exists on both sides, so the two map onto
    one shape; see `stashdb_mapping`.

Whisparr v3 is no guide here despite indexing the same data: it does not talk
to StashDB at all, it consumes its own proxy at `api.whisparr.com/v3/` and
receives generic Sonarr-shaped resources. There is no StashDB parsing in it to
follow, which is why this is written against the published schema instead.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import requests
from loguru import logger

from program.settings import settings_manager


class StashdbApiError(Exception):
    """A StashDB query that did not return usable data."""


# Asked for on every scene, in one place so search and detail cannot drift.
#
# `images` is sorted largest-first by the mapper rather than here: StashDB
# returns them unordered and there is no "primary" flag to ask for.
_SCENE_FIELDS = """
    id
    title
    details
    release_date
    code
    duration
    urls { url site { name } }
    studio { id name parent { id name } }
    performers { as performer { id name gender } }
    tags { id name }
    images { id url width height }
"""

# `searchScenes` does NOT return a list of scenes. It returns
# QueryScenesResultType, a `{count, scenes}` wrapper -- so asking for scene
# fields at the top level is rejected outright, with one
# GRAPHQL_VALIDATION_FAILED per field and an HTTP 422 rather than a partial
# result. `findScene` below is the asymmetric one: it returns a Scene
# directly.
_SEARCH_QUERY = f"""
query Search($term: String!, $limit: Int!) {{
    searchScenes(term: $term, limit: $limit) {{
        count
        scenes {{
            {_SCENE_FIELDS}
        }}
    }}
}}
"""

_SCENE_QUERY = f"""
query Scene($id: ID!) {{
    findScene(id: $id) {{
        {_SCENE_FIELDS}
    }}
}}
"""


class StashdbApi:
    """A thin, cached GraphQL client for StashDB."""

    def __init__(self) -> None:
        self.settings = settings_manager.settings.stashdb
        self.session = requests.Session()

    # --- plumbing --------------------------------------------------------

    @property
    def configured(self) -> bool:
        """Whether this provider can be used at all.

        Checked by callers BEFORE counting a provider as having failed: a
        provider with no key has not been tried, and treating it as a failed
        attempt would make the fallback chain report an error for something
        the user simply has not set up.
        """

        return bool(self.settings.enabled and self.settings.api_key)

    def _cache_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()

        return Path(self.settings.cache_dir) / f"{digest}.json"

    def _cache_get(self, key: str) -> dict[str, Any] | None:
        if not self.settings.cache_enabled:
            return None

        path = self._cache_path(key)

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

        ttl = self.settings.cache_ttl_seconds

        if ttl and time.time() - float(payload.get("stored_at", 0)) > ttl:
            return None

        body = payload.get("body")

        return body if isinstance(body, dict) else None

    def _cache_put(self, key: str, body: dict[str, Any]) -> None:
        if not self.settings.cache_enabled:
            return

        path = self._cache_path(key)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"stored_at": time.time(), "body": body}),
                encoding="utf-8",
            )
        except OSError as e:
            # A cache that cannot be written is a slower provider, not a
            # broken one.
            logger.debug(f"Could not cache the StashDB response: {e}")

    def _query(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Run one GraphQL query and return its `data`, or raise."""

        if not self.configured:
            raise StashdbApiError("StashDB is not configured")

        cache_key = json.dumps([query, variables], sort_keys=True)
        cached = self._cache_get(cache_key)

        if cached is not None:
            return cached

        try:
            response = self.session.post(
                self.settings.api_url,
                json={"query": query, "variables": variables},
                headers={
                    "ApiKey": self.settings.api_key,
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
        except requests.RequestException as e:
            raise StashdbApiError(f"StashDB is unreachable: {e}") from e

        if not response.ok:
            raise StashdbApiError(f"StashDB returned HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as e:
            raise StashdbApiError("StashDB returned a non-JSON body") from e

        errors = payload.get("errors")

        if errors:
            message = "; ".join(
                str(error.get("message", "unknown")) for error in errors
            )

            # Named explicitly because it is the one failure a user can fix,
            # and the generic text ("not authorized") does not say where to
            # look.
            if "not authorized" in message.lower():
                raise StashdbApiError(
                    "StashDB rejected the API key. Check Settings -> StashDB; "
                    "the key is on your StashDB account page."
                )

            raise StashdbApiError(f"StashDB query failed: {message}")

        data = payload.get("data")

        if not isinstance(data, dict):
            raise StashdbApiError("StashDB returned no data")

        self._cache_put(cache_key, data)

        return data

    # --- the two calls anything here needs --------------------------------

    def search_scenes(self, term: str, limit: int = 10) -> list[dict[str, Any]]:
        """Fuzzy search. Returns raw scene dicts, newest-ranked as StashDB ranks them."""

        if not term.strip():
            return []

        data = self._query(_SEARCH_QUERY, {"term": term, "limit": limit})
        result = data.get("searchScenes")
        scenes = result.get("scenes") if isinstance(result, dict) else None

        return [scene for scene in (scenes or []) if isinstance(scene, dict)]

    def get_scene(self, scene_id: str) -> dict[str, Any] | None:
        """One scene by its StashDB UUID."""

        if not scene_id:
            return None

        data = self._query(_SCENE_QUERY, {"id": scene_id})
        scene = data.get("findScene")

        return scene if isinstance(scene, dict) else None
