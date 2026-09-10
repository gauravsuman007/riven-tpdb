"""StashDB indexer.

The mirror of `tpdb_indexer` for items carrying a `stashdb_id`. It is
deliberately the same shape and produces the same `Movie` dict, so nothing
downstream can tell which provider an item came from -- see
`stashdb_mapping` for the mapping itself.

StashDB has scenes only; there is no separate movie endpoint to try, so this
is one lookup rather than the two the TPDB indexer needs.
"""

from typing import Any

from kink import di
from loguru import logger

from program.apis.stashdb_api import StashdbApi, StashdbApiError
from program.core.runner import MediaItemGenerator, RunnerResult
from program.media.item import MediaItem, Movie
from program.services.indexers.base import BaseIndexer
from program.services.indexers.stashdb_mapping import scene_to_movie_dict
from program.utils.time import utcnow


class StashDBIndexer(BaseIndexer):
    """Resolves StashDB scenes into `Movie` items."""

    def __init__(self):
        super().__init__()

        self.api = di[StashdbApi]

    def run(self, item: MediaItem, log_msg: bool = True) -> MediaItemGenerator[Movie]:
        if not item.stashdb_id:
            logger.error(
                f"Item {item.log_string} has no stashdb_id, cannot index it"
            )
            return

        if item.type not in ["movie", "mediaitem"]:
            logger.debug(f"StashDB indexer skipping item type: {item.log_string}")
            return

        try:
            scene = self.api.get_scene(item.stashdb_id)
        except StashdbApiError as e:
            # Not an error-level line: an unreachable provider is a transient
            # condition the retry pass will come back to, and logging it as a
            # failure makes an outage look like corrupt data.
            logger.debug(f"StashDB unavailable for {item.log_string}: {e}")
            return

        if scene is None:
            logger.debug(f"No StashDB scene for id {item.stashdb_id}")
            return

        data = scene_to_movie_dict(scene)

        if item.type == "mediaitem":
            indexed = self.copy_items(item, Movie(data))
            indexed.indexed_at = utcnow()

            if log_msg:
                logger.debug(
                    f"Indexed Movie {indexed.log_string} "
                    f"(StashDB: {indexed.stashdb_id})"
                )

            yield RunnerResult(media_items=[indexed])
            return

        if isinstance(item, Movie):
            self._apply(item, data)
            item.indexed_at = utcnow()

            if log_msg:
                logger.debug(
                    f"Re-indexed Movie {item.log_string} (StashDB: {item.stashdb_id})"
                )

            yield RunnerResult(media_items=[item])
            return

        logger.error(f"Failed to index item with stashdb_id: {item.stashdb_id}")

    @staticmethod
    def _apply(movie: Movie, data: dict[str, Any]) -> None:
        """Apply mapped StashDB data onto an existing `Movie`.

        `tpdb_id` is never touched. An item can legitimately hold both ids --
        matched on StashDB first, then found on TPDB later -- and clearing one
        because the other refreshed would lose a match that cost a lookup to
        find.
        """

        movie.title = data["title"]
        movie.poster_path = data["poster_path"]
        movie.year = data["year"]
        movie.stashdb_id = data["stashdb_id"]
        movie.site_id = data["site_id"]
        movie.site_name = data["site_name"]
        movie.performers = data["performers"]
        movie.genres = data["genres"]
        movie.aired_at = data["aired_at"]
        movie.rating = data["rating"]
