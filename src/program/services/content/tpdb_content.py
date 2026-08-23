"""TPDB content module. Provides site-based adult content subscriptions."""

from kink import di
from loguru import logger

from program.apis.tpdb_api import TpdbApi
from program.core.runner import MediaItemGenerator, Runner, RunnerResult
from program.db.db_functions import item_exists_by_any_id
from program.media.item import MediaItem
from program.settings import settings_manager
from program.settings.models import TpdbContentModel

PAGE_SIZE = 20


class TPDBContent(Runner[TpdbContentModel]):
    """Content service that fetches new adult scenes from subscribed TPDB sites."""

    is_content_service = True

    def __init__(self):
        super().__init__()

        self.settings = settings_manager.settings.content.tpdb

        if not self.enabled:
            return

        self.api = di[TpdbApi]
        self.initialized = self.validate()

        if not self.initialized:
            return

        logger.success("TPDB content initialized!")

    @classmethod
    def get_key(cls) -> str:
        return "tpdb"

    def validate(self) -> bool:
        """Validate TPDB content settings."""

        if not self.settings.enabled:
            return False

        if not settings_manager.settings.tpdb.api_token:
            logger.error("TPDB API token is not set.")
            return False

        if not self.settings.sites:
            logger.error("No TPDB sites configured.")
            return False

        return True

    def run(self, item: MediaItem) -> MediaItemGenerator:
        """Fetch new scenes from all subscribed TPDB sites."""

        try:
            tpdb_items = list[MediaItem]()
            seen = set[str]()

            for site_id in self.settings.sites:
                for scene in self._iter_site_scenes(site_id):
                    tpdb_id = scene.id

                    if not tpdb_id or tpdb_id in seen:
                        continue

                    seen.add(tpdb_id)

                    if item_exists_by_any_id(tpdb_id=tpdb_id):
                        continue

                    tpdb_items.append(
                        MediaItem(
                            {
                                "tpdb_id": tpdb_id,
                                "requested_by": self.key,
                            }
                        )
                    )
        except Exception as e:
            logger.error(f"Failed to fetch items from TPDB: {e}")
            return

        logger.info(f"Fetched {len(tpdb_items)} new items from TPDB")

        yield RunnerResult(media_items=tpdb_items)

    def _iter_site_scenes(self, site_id: str):
        """Page through recent scenes for a site (bounded by ``max_pages``)."""

        for page in range(1, self.settings.max_pages + 1):
            scenes = self.api.list_scenes(site_id=site_id, page=page)

            yield from scenes

            if len(scenes) < PAGE_SIZE:
                return