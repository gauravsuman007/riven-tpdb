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

from typing import Any
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field

from program.utils.request import SmartSession


class TpdbApiError(Exception):
    """Base exception for TPDB API errors."""


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

    def __init__(self, api_base_url: str | None = None, api_token: str = ""):
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

        response = self.session.get(url)

        if response.status_code >= 400:
            raise TpdbApiError(
                f"TPDB request failed ({response.status_code}): {response.text[:200]}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise TpdbApiError("TPDB returned non-JSON response") from exc

        if not isinstance(data, dict):
            raise TpdbApiError(f"Unexpected TPDB response type: {type(data).__name__}")

        if "message" in data:
            raise TpdbApiError(str(data["message"]))

        return data

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
        site_id: str | None = None,
        page: int | None = None,
    ) -> list[TpdbScene]:
        """List scenes, optionally filtered by site UUID and paginated.

        The live API returns a fixed page size (20); use ``page`` to advance.
        """

        data = self._get("scenes", {"site": site_id, "page": page})
        return self._parse_many(data, TpdbScene)

    def get_scene(self, uuid: str) -> TpdbScene | None:
        data = self._get(f"scenes/{uuid}")
        return self._parse_one(data, TpdbScene)

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
        data = self._get(f"movies/{movie_id}")
        return self._parse_one(data, TpdbMovie)

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