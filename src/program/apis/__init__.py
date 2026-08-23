from kink import di

from program.settings import settings_manager

from .plex_api import PlexAPI
from .tmdb_api import TMDBApi
from .tvdb_api import TVDBApi
from .tpdb_api import TpdbApi


def bootstrap_apis():
    __setup_plex()
    __setup_tmdb()
    __setup_tvdb()
    __setup_tpdb()


def __setup_tmdb():
    di[TMDBApi] = TMDBApi()


def __setup_tvdb():
    di[TVDBApi] = TVDBApi()


def __setup_tpdb():
    di[TpdbApi] = TpdbApi(
        api_base_url=settings_manager.settings.tpdb.api_base_url,
        api_token=settings_manager.settings.tpdb.api_token,
    )


def __setup_plex():
    if not settings_manager.settings.updaters.plex.enabled:
        return

    di[PlexAPI] = PlexAPI(
        settings_manager.settings.updaters.plex.token,
        settings_manager.settings.updaters.plex.url,
    )
