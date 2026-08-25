"""ThePornDB (TPDB) API client.

TPDB is the adult metadata database backing Whisparr, Stash and several
media-server metadata plugins. This client talks to TPDB directly so the
fork has no dependency on Whisparr itself.

Contract (see ThePornDatabase/Jellyfin.Plugin.ThePornDB for reference):

    Base URL: https://api.theporndb.net
    Auth:     Authorization: Bearer <api_token>  (sent only when configured)
    Response: {"data": [...]} or {"data": {...}}; errors return {"message": ...}

Endpoints used here:

    GET /scenes?parse={title}&hash={oshash}&year={year}   -> list[Scene]
    GET /scenes/{uuid}                                    -> Scene
    GET /movies?parse={title}&hash={oshash}&year={year}   -> list[Movie]
    GET /movies/{id}                                      -> Movie
    GET /performers?q={name}                              -> list[Performer]
    GET /performers/{id}                                  -> Performer
    GET /sites?q={name}                                   -> list[Site]
    GET /sites/{id}                                       -> Site
"""

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from program.utils.request import SmartSession


class TpdbApiError(Exception):
    """Base exception for TPDB API errors.

    ``status_code`` carries the upstream HTTP status when the failure came from
    a response rather than from parsing, so callers can distinguish "no such
    record" from "TPDB is unwell".
    """

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class _TpdbModel(BaseModel):
    """Base model that preserves unknown fields through ``model_dump()``.

    TPDB responses include fields beyond the ones we care about; keeping them
    ensures the indexer mapping can read nested data such as ``site.uuid``.
    """

    model_config = ConfigDict(extra="allow")


class TpdbImage(_TpdbModel):
    full: str | None = None
    large: str | None = None


class TpdbSiteParent(_TpdbModel):
    """Parent/network reference for a site (studio)."""

    id: int | None = None
    name: str | None = None


class TpdbSite(_TpdbModel):
    """A TPDB site, a.k.a. studio/network."""

    id: int | None = None
    uuid: str | None = None
    name: str | None = None
    poster: str | None = None
    logo: str | None = None
    favicon: str | None = None
    # Often a single line ("Tushy is a part of the Vixen Media Group network")
    # and sometimes empty. Declared rather than left to `extra="allow"` so the
    # studio directory can rely on it being a real, typed field.
    description: str | None = None
    parent_id: int | None = None
    network_id: int | None = None
    parent: TpdbSiteParent | None = None
    network: TpdbSiteParent | None = None


class TpdbPerformerExtras(_TpdbModel):
    gender: str | None = None


class TpdbPerformer(_TpdbModel):
    """A performer (actor)."""

    id: str | None = None
    name: str | None = None
    disambiguation: str | None = None
    face: str | None = None
    image: str | None = None
    extras: TpdbPerformerExtras | None = None
    parent: Any | None = None


class TpdbDirector(_TpdbModel):
    # `id` is an int in the live API (despite the plugin's C# model typing it
    # as a string), so accept both.
    id: int | str | None = None
    name: str | None = None


class TpdbTag(_TpdbModel):
    """A tag (genre)."""

    id: int | None = None
    name: str | None = None


class TpdbScene(_TpdbModel):
    """A single scene."""

    id: str | None = None  # UUID
    title: str | None = None
    description: str | None = None
    rating: float | None = None
    trailer: str | None = None
    date: str | None = None
    duration: int | None = None
    site: TpdbSite | None = None
    performers: list[TpdbPerformer] = Field(default_factory=list)
    directors: list[TpdbDirector] = Field(default_factory=list)
    tags: list[TpdbTag] = Field(default_factory=list)
    poster: str | None = None
    background: TpdbImage | None = None
    background_back: TpdbImage | None = None
    posters: TpdbImage | None = None


class TpdbMovie(_TpdbModel):
    """A full-length movie."""

    id: str | None = None
    title: str | None = None
    description: str | None = None
    rating: float | None = None
    date: str | None = None
    duration: int | None = None
    site: TpdbSite | None = None
    performers: list[TpdbPerformer] = Field(default_factory=list)
    directors: list[TpdbDirector] = Field(default_factory=list)
    tags: list[TpdbTag] = Field(default_factory=list)
    poster: str | None = None
    background: TpdbImage | None = None
    posters: TpdbImage | None = None


class TpdbApi:
    """Handles ThePornDB API communication."""

    BASE_URL = "https://api.theporndb.net"

    # Catalogue and similar-title lists barely move minute to minute, but a
    # single home or detail page can ask for the same ones several times over,
    # and every miss is charged against a 2-per-second rate limit. The disk
    # tier is the real cache; the memory tier just avoids re-reading files
    # within one request.
    MEMORY_MAX_ENTRIES = 512
    SLOW_REQUEST_SECONDS = 1.0

    def __init__(
        self,
        api_base_url: str | None = None,
        api_token: str = "",
        cache_ttl: float | None = None,
        cache_dir: Path | str | None = None,
        cache_max_size_mb: int | None = None,
        cache_enabled: bool = True,
    ):
        base_url = (api_base_url or self.BASE_URL).rstrip("/")

        self.session = SmartSession(
            base_url=base_url,
            rate_limits={
                # TPDB is strict about rate limits; stay conservative.
                "api.theporndb.net": {"rate": 2, "capacity": 5},
            },
            retries=2,
            backoff_factor=0.3,
        )

        self.session.headers.update({"Accept": "application/json"})

        if api_token:
            self.session.headers.update({"Authorization": f"Bearer {api_token}"})

        self._site_id_cache: dict[str, int | None] = {}

        self.cache_enabled = cache_enabled
        self.cache_ttl = 300.0 if cache_ttl is None else float(cache_ttl)
        self.cache_max_bytes = (
            250 if cache_max_size_mb is None else cache_max_size_mb
        ) * 1024 * 1024

        # url -> (wall-clock stored_at, body). Wall clock, not monotonic, so a
        # disk entry written by an earlier process can be aged consistently.
        self._memory: dict[str, tuple[float, dict[str, Any]]] = {}
        self._cache_lock = threading.Lock()
        self._cache_dir: Path | None = None

        if self.cache_enabled and cache_dir:
            candidate = Path(cache_dir)

            try:
                candidate.mkdir(parents=True, exist_ok=True)
                self._cache_dir = candidate
            except OSError as exc:
                # An unwritable directory must degrade to memory-only rather
                # than take the whole integration down.
                logger.warning(
                    f"TPDB cache directory {candidate} is unusable ({exc}); "
                    "falling back to an in-memory cache"
                )

    def _is_fresh(self, stored_at: float) -> bool:
        """Whether an entry stored at `stored_at` (wall clock) is still fresh.

        A ttl of 0 means "never expires" -- the size limit is then the only
        thing that evicts, which is what a size-managed cache implies.
        """

        if self.cache_ttl <= 0:
            return True

        return (time.time() - stored_at) <= self.cache_ttl

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self._cache_dir / f"{digest}.json"

    def _cache_get(
        self, url: str, *, allow_stale: bool = False
    ) -> dict[str, Any] | None:
        """Return a cached body for `url`, or None when absent or stale.

        `allow_stale` ignores the TTL. It is used only when the live call has
        already failed: an out-of-date title beats a 502 on a catalogue whose
        records barely change.
        """

        if not self.cache_enabled:
            return None

        with self._cache_lock:
            hit = self._memory.get(url)

            if hit is not None:
                stored_at, body = hit

                if allow_stale or self._is_fresh(stored_at):
                    return body

                self._memory.pop(url, None)

        if self._cache_dir is None:
            return None

        path = self._cache_path(url)

        try:
            with path.open("r", encoding="utf-8") as handle:
                entry = json.load(handle)

            stored_at = float(entry["stored_at"])
            body = entry["body"]
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # A truncated or hand-edited entry is not worth failing a page for.
            logger.debug(f"Discarding unreadable TPDB cache entry {path.name}: {exc}")
            self._discard(path)
            return None

        if not self._is_fresh(stored_at):
            # Keep the file when a stale read was asked for -- it is the only
            # copy of the answer, and discarding it would turn the next outage
            # into a hard failure.
            if not allow_stale:
                self._discard(path)
                return None

        if not isinstance(body, dict):
            self._discard(path)
            return None

        # Touch so size eviction treats this as recently used.
        try:
            os.utime(path, None)
        except OSError:
            pass

        with self._cache_lock:
            self._memory[url] = (stored_at, body)

        return body

    def _cache_put(self, url: str, body: dict[str, Any]) -> None:
        if not self.cache_enabled:
            return

        stored_at = time.time()

        with self._cache_lock:
            if len(self._memory) >= self.MEMORY_MAX_ENTRIES:
                # The memory tier is only a read-through shortcut; the disk
                # tier is the real cache, so clearing it costs a file read at
                # worst and beats tracking per-entry LRU order in memory.
                self._memory.clear()

            self._memory[url] = (stored_at, body)

        if self._cache_dir is None:
            return

        path = self._cache_path(url)
        payload = {"url": url, "stored_at": stored_at, "body": body}

        try:
            # Write via a temporary file so a crash mid-write cannot leave a
            # half-written entry that every later read has to discard.
            tmp = path.with_suffix(".tmp")

            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle)

            tmp.replace(path)
        except (OSError, TypeError, ValueError) as exc:
            logger.debug(f"Could not write TPDB cache entry: {exc}")
            return

        self._enforce_size_limit()

    @staticmethod
    def _discard(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    def _enforce_size_limit(self) -> None:
        """Evict least recently used entries until the cache fits its budget."""

        if self._cache_dir is None or self.cache_max_bytes <= 0:
            return

        try:
            entries = list[tuple[float, int, Path]]()
            total = 0

            for path in self._cache_dir.glob("*.json"):
                try:
                    stat = path.stat()
                except OSError:
                    continue

                entries.append((stat.st_mtime, stat.st_size, path))
                total += stat.st_size

            if total <= self.cache_max_bytes:
                return

            # Oldest touch first: least recently used goes first.
            entries.sort(key=lambda entry: entry[0])

            for _, size, path in entries:
                if total <= self.cache_max_bytes:
                    break

                self._discard(path)
                total -= size

            logger.debug(
                f"TPDB cache trimmed to {total // 1024} KiB "
                f"(limit {self.cache_max_bytes // 1024} KiB)"
            )
        except OSError as exc:
            logger.debug(f"TPDB cache size enforcement failed: {exc}")

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = path

        if params:
            clean = {key: value for key, value in params.items() if value not in (None, "")}

            if clean:
                url = f"{path}?{urlencode(clean)}"

        cached = self._cache_get(url)

        if cached is not None:
            return cached

        def stale_or_raise(exc: Exception) -> dict[str, Any]:
            """Fall back to an expired entry when the live call fails."""

            fallback = self._cache_get(url, allow_stale=True)

            if fallback is not None:
                logger.warning(
                    f"TPDB request for {url} failed ({exc}); serving a stale "
                    "cached response instead"
                )
                return fallback

            raise exc

        # TPDB is rate limited to a couple of requests a second, so a page that
        # fans out over the collection spends most of its time waiting in the
        # bucket rather than on the wire. Timing every call makes that visible
        # instead of leaving "the UI is slow" unattributable.
        started = time.monotonic()

        try:
            response = self.session.get(url)
        except Exception as exc:
            return stale_or_raise(
                TpdbApiError(f"TPDB request failed: {exc}")
            )

        elapsed = time.monotonic() - started

        if elapsed > self.SLOW_REQUEST_SECONDS:
            logger.debug(f"TPDB GET {url} took {elapsed:.2f}s")

        if response.status_code >= 400:
            error = TpdbApiError(
                f"TPDB request failed ({response.status_code}): {response.text[:200]}",
                status_code=response.status_code,
            )

            # A 404 is a real answer -- the title does not exist -- so it must
            # propagate. Only outages fall back to stale data.
            if response.status_code == 404:
                raise error

            return stale_or_raise(error)

        try:
            data = response.json()
        except ValueError as exc:
            raise TpdbApiError("TPDB returned non-JSON response") from exc

        if not isinstance(data, dict):
            raise TpdbApiError(f"Unexpected TPDB response type: {type(data).__name__}")

        if "message" in data:
            raise TpdbApiError(str(data["message"]))

        self._cache_put(url, data)

        return data

    def _get_optional(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Like `_get`, but a 404 means "no such record" rather than a failure.

        Lookups by id are routinely speculative -- a TPDB uuid may name either
        a scene or a movie, so callers try one and fall back to the other. A
        raised 404 would abort that fallback before it ran.
        """

        try:
            return self._get(path, params)
        except TpdbApiError as exc:
            if exc.status_code == 404:
                return None
            raise

    @staticmethod
    def _parse_one(data: dict[str, Any], model: type[BaseModel]):
        payload = data.get("data")

        if not isinstance(payload, dict):
            return None

        return model.model_validate(payload)

    @staticmethod
    def _parse_many(data: dict[str, Any], model: type[BaseModel]) -> list[BaseModel]:
        payload = data.get("data")

        if not isinstance(payload, list):
            return []

        return [model.model_validate(item) for item in payload]

    # Scenes

    def search_scenes(
        self,
        title: str | None = None,
        oshash: str | None = None,
        year: int | None = None,
    ) -> list[TpdbScene]:
        data = self._get("scenes", {"parse": title, "hash": oshash, "year": year})
        return self._parse_many(data, TpdbScene)

    def list_scenes(
        self,
        site_id: str | int | None = None,
        page: int | None = None,
        per_page: int | None = None,
    ) -> list[TpdbScene]:
        """List scenes newest-first, optionally restricted to one site.

        ``site_id`` accepts a site UUID or numeric id; UUIDs are resolved to the
        numeric id first because the API only filters on the latter.

        NOTE: the filter parameter is ``site_id``. A plain ``site`` parameter is
        accepted by the API but silently ignored, which returns the unfiltered
        global feed rather than an error.
        """

        resolved = self.resolve_site_id(site_id) if site_id is not None else None
        data = self._get(
            "scenes",
            {"site_id": resolved, "page": page, "per_page": per_page},
        )
        return self._parse_many(data, TpdbScene)

    def list_movies(
        self,
        site_id: str | int | None = None,
        page: int | None = None,
        per_page: int | None = None,
    ) -> list[TpdbMovie]:
        """List movies newest-first, optionally restricted to one site."""

        resolved = self.resolve_site_id(site_id) if site_id is not None else None
        data = self._get(
            "movies",
            {"site_id": resolved, "page": page, "per_page": per_page},
        )
        return self._parse_many(data, TpdbMovie)

    def resolve_site_id(self, site_ref: str | int) -> int | None:
        """Resolve a site UUID (or numeric id) to the numeric id used by filters.

        ``/sites/{id}`` accepts either form, so a UUID costs one extra lookup.
        Results are memoised for the lifetime of the client.
        """

        if isinstance(site_ref, int) or str(site_ref).isdigit():
            return int(site_ref)

        key = str(site_ref)

        if key in self._site_id_cache:
            return self._site_id_cache[key]

        site = self.get_site(key)
        resolved = site.id if site else None
        self._site_id_cache[key] = resolved

        return resolved

    def get_scene(self, uuid: str) -> TpdbScene | None:
        data = self._get_optional(f"scenes/{uuid}")
        return self._parse_one(data, TpdbScene) if data else None

    # Movies

    def search_movies(
        self,
        title: str | None = None,
        oshash: str | None = None,
        year: int | None = None,
    ) -> list[TpdbMovie]:
        data = self._get("movies", {"parse": title, "hash": oshash, "year": year})
        return self._parse_many(data, TpdbMovie)

    def get_movie(self, movie_id: str) -> TpdbMovie | None:
        data = self._get_optional(f"movies/{movie_id}")
        return self._parse_one(data, TpdbMovie) if data else None

    def search_scenes_text(
        self,
        query: str,
        page: int | None = None,
        per_page: int | None = None,
    ) -> list[TpdbScene]:
        """Full-text scene search.

        ``q`` is the only parameter the API actually filters scenes on; tag
        filters (``tag``/``tags``/``tags[]``/``filter[tags]``) are either
        ignored or return nothing, so tag browsing is built on this.
        """

        data = self._get("scenes", {"q": query, "page": page, "per_page": per_page})
        return self._parse_many(data, TpdbScene)

    def search_movies_text(
        self,
        query: str,
        page: int | None = None,
        per_page: int | None = None,
    ) -> list[TpdbMovie]:
        """Full-text movie search."""

        data = self._get("movies", {"q": query, "page": page, "per_page": per_page})
        return self._parse_many(data, TpdbMovie)

    # Similar / related

    def get_similar_movies(self, movie_id: str) -> list[TpdbMovie]:
        """Movies TPDB considers related to ``movie_id``.

        This is the same "related" list the TPDB website shows on a title page.
        """

        data = self._get(f"movies/{movie_id}/similar")
        return self._parse_many(data, TpdbMovie)

    def get_similar_scenes(self, scene_id: str) -> list[TpdbScene]:
        """Scenes TPDB considers related to ``scene_id``."""

        data = self._get(f"scenes/{scene_id}/similar")
        return self._parse_many(data, TpdbScene)

    # Collection (the signed-in user's own marked titles)

    def list_collected_movies(
        self,
        page: int | None = None,
        per_page: int | None = None,
    ) -> list[TpdbMovie]:
        """Movies in the authenticated user's TPDB collection.

        ``is_collected`` is the working filter. ``collected`` and
        ``in_collection`` are accepted but ignored, returning the unfiltered
        feed.
        """

        data = self._get(
            "movies", {"is_collected": "true", "page": page, "per_page": per_page}
        )
        return self._parse_many(data, TpdbMovie)

    def list_collected_scenes(
        self,
        page: int | None = None,
        per_page: int | None = None,
    ) -> list[TpdbScene]:
        """Scenes in the authenticated user's TPDB collection."""

        data = self._get(
            "scenes", {"is_collected": "true", "page": page, "per_page": per_page}
        )
        return self._parse_many(data, TpdbScene)

    def invalidate_cache(self) -> None:
        """Drop every cached read. Called after a write that changes them."""

        with self._cache_lock:
            self._memory.clear()

        if self._cache_dir is None:
            return

        try:
            for path in self._cache_dir.glob("*.json"):
                self._discard(path)
        except OSError as exc:
            logger.debug(f"Could not clear TPDB cache directory: {exc}")

    def numeric_id(self, uuid: str, kind: str = "movie") -> int | None:
        """The integer ``_id`` behind a TPDB uuid.

        The collection routes are keyed on this, not on the uuid every other
        endpoint uses. It is read from the raw payload rather than a parsed
        model because pydantic does not surface underscore-prefixed keys as
        extra fields, so ``TpdbMovie`` cannot carry it however permissive the
        model config is.
        """

        path = f"scenes/{uuid}" if kind == "scene" else f"movies/{uuid}"
        data = self._get_optional(path)
        payload = (data or {}).get("data")

        if not isinstance(payload, dict):
            return None

        value = payload.get("_id")

        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def is_collected(self, numeric_id: int | str) -> bool:
        """Whether one title is in the collection.

        Takes the *integer* ``_id``, not the UUID. Despite the parameter being
        named ``scene_id`` the id space is shared, so movies work here too.
        """

        data = self._get("user/collection", {"scene_id": numeric_id})
        return bool(data.get("value"))

    def add_to_collection(self, numeric_id: int | str) -> bool:
        """Add one title to the collection, by integer ``_id``.

        NOTE: TPDB exposes no DELETE on this route (GET, HEAD, POST only), so
        this cannot be undone through the API -- removal is a manual step on the
        TPDB website.
        """

        response = self.session.post(
            "user/collection", json={"scene_id": int(numeric_id)}
        )

        if response.status_code >= 400:
            error = TpdbApiError(
                f"TPDB request failed ({response.status_code}): {response.text[:200]}",
                status_code=response.status_code,
            )

            # A 404 is a real answer -- the title does not exist -- so it must
            # propagate. Only outages fall back to stale data.
            if response.status_code == 404:
                raise error

            return stale_or_raise(error)

        # Adding invalidates every cached read: the collection lists change,
        # `is_collected` flips, and recommendations are derived from both. A
        # stale hit would show the title as uncollected immediately after the
        # user collected it. Cleared after the write lands, not before, so a
        # concurrent read cannot repopulate the old value.
        self.invalidate_cache()

        return True

    # Tags

    def list_tags(
        self,
        page: int | None = None,
        per_page: int | None = None,
    ) -> list[TpdbTag]:
        """List the tag vocabulary (~2.6k entries, paginated)."""

        data = self._get("tags", {"page": page, "per_page": per_page})
        return self._parse_many(data, TpdbTag)

    # Performers

    def search_performers(self, query: str) -> list[TpdbPerformer]:
        data = self._get("performers", {"q": query})
        return self._parse_many(data, TpdbPerformer)

    def get_performer(self, performer_id: str) -> TpdbPerformer | None:
        data = self._get(f"performers/{performer_id}")
        return self._parse_one(data, TpdbPerformer)

    # Sites (studios/networks)

    def search_sites(self, query: str) -> list[TpdbSite]:
        data = self._get("sites", {"q": query})
        return self._parse_many(data, TpdbSite)

    def get_site(self, site_id: str) -> TpdbSite | None:
        data = self._get(f"sites/{site_id}")
        return self._parse_one(data, TpdbSite)