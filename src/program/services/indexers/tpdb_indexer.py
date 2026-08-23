"""ThePornDB (TPDB) indexer module.

Resolves adult scenes and movies from TPDB into Riven `Movie` items. The
fork is adult-only, so this is the only metadata indexer in use.
"""

from datetime import datetime
from typing import Any

from kink import di
from loguru import logger

from program.apis.tpdb_api import TpdbApi
from program.core.runner import MediaItemGenerator, RunnerResult
from program.media.item import MediaItem, Movie
from program.services.indexers.base import BaseIndexer
from program.services.indexers.tpdb_mapping import (
    movie_to_movie_dict,
    scene_to_movie_dict,
)


class TPDBIndexer(BaseIndexer):
    """Indexer that resolves TPDB scenes and movies into `Movie` items."""

    def __init__(self):
        super().__init__()

        self.api = di[TpdbApi]

    def run(
        self,
        item: MediaItem,
        log_msg: bool = True,
    ) -> MediaItemGenerator[Movie]:
        """Run the TPDB indexer for the given item."""

        if not item.tpdb_id:
            logger.error(
                f"Item {item.log_string} does not have a tpdb_id, cannot index it"
            )
            return

        if item.type not in ["movie", "mediaitem"]:
            logger.debug(
                f"TPDB indexer skipping incorrect item type: {item.log_string}"
            )
            return

        # Fresh indexing: create a new Movie from TPDB data
        if item.type == "mediaitem":
            if indexed_item := self._create_movie_from_id(item.tpdb_id):
                indexed_item = self.copy_items(item, indexed_item)
                indexed_item.indexed_at = datetime.now()
                if log_msg:
                    logger.debug(
                        f"Indexed Movie {indexed_item.log_string} "
                        f"(TPDB: {indexed_item.tpdb_id})"
                    )

                yield RunnerResult(media_items=[indexed_item])
                return

        # Re-indexing an existing Movie in place
        elif isinstance(item, Movie):
            if self._update_movie_metadata(item):
                item.indexed_at = datetime.now()
                if log_msg:
                    logger.debug(
                        f"Re-indexed Movie {item.log_string} (TPDB: {item.tpdb_id})"
                    )

                yield RunnerResult(media_items=[item])
                return

        logger.error(f"Failed to index item with tpdb_id: {item.tpdb_id}")
        return

    def _create_movie_from_id(self, tpdb_id: str) -> Movie | None:
        """Create a Movie from a TPDB id (tries scenes first, then movies)."""

        if scene := self.api.get_scene(tpdb_id):
            return self._movie_from_dict(scene_to_movie_dict(scene.model_dump()))

        if movie := self.api.get_movie(tpdb_id):
            return self._movie_from_dict(movie_to_movie_dict(movie.model_dump()))

        logger.debug(f"No TPDB scene or movie found for id {tpdb_id}")
        return None

    def _update_movie_metadata(self, movie: Movie) -> bool:
        """Update an existing Movie with fresh TPDB metadata."""

        if not movie.tpdb_id:
            logger.error(f"Movie {movie.log_string} has no TPDB id")
            return False

        data = None

        if scene := self.api.get_scene(movie.tpdb_id):
            data = scene_to_movie_dict(scene.model_dump())
        elif tpdb_movie := self.api.get_movie(movie.tpdb_id):
            data = movie_to_movie_dict(tpdb_movie.model_dump())

        if not data:
            logger.error(f"No TPDB data found for id {movie.tpdb_id}")
            return False

        self._apply_movie_data(movie, data)
        return True

    @staticmethod
    def _movie_from_dict(data: dict[str, Any]) -> Movie:
        """Build a `Movie` instance from a mapped dict."""

        return Movie(data)

    @staticmethod
    def _apply_movie_data(movie: Movie, data: dict[str, Any]) -> None:
        """Apply mapped TPDB data onto an existing `Movie`."""

        movie.title = data["title"]
        movie.poster_path = data["poster_path"]
        movie.year = data["year"]
        movie.tpdb_id = data["tpdb_id"]
        movie.site_id = data["site_id"]
        movie.site_name = data["site_name"]
        movie.performers = data["performers"]
        movie.genres = data["genres"]
        movie.aired_at = data["aired_at"]
        movie.rating = data["rating"]
