"""The Jellyfin-compatible API, so the library plays on a television.

Mounted at the application ROOT, not under /api/v1: Jellyfin clients construct
absolute paths like /Users/AuthenticateByName and /System/Info/Public, and a
prefix is not something they can be told about.

Auth is handled per-route rather than by a router-level dependency, because
three of these endpoints must answer while the caller is still unauthenticated
(discovery, the public system info, and the login itself). See
`services/jellyfin_server/auth.py` for why the token is the Riven API key.

The endpoint set here is the one real clients were observed to need, not the
whole documented API -- see AGENTS.md. Adding an endpoint because a client
asked for it is the intended way for this file to grow; adding one because the
OpenAPI spec lists it is not.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from loguru import logger
import sqlalchemy
from sqlalchemy import func, or_, select

from program.db.db import db_session
from program.media.item import MediaItem
from program.services.jellyfin_server import auth, capabilities, ids, mapping
from program.services.streaming import transcode
from program.settings import settings_manager

router = APIRouter(tags=["jellyfin"], include_in_schema=False)

#: Reported to clients as the server version. Clients gate features on this,
#: so it names a real Jellyfin release whose behaviour this surface actually
#: implements -- claiming something newer turns on client code paths that then
#: 404. Raise it only alongside the features that justify it.
ADVERTISED_VERSION = "10.10.3"


def require_enabled() -> None:
    if not settings_manager.settings.jellyfin_server.enabled:
        raise HTTPException(status_code=404, detail="Not found")


def require_auth(request: Request) -> auth.ClientIdentity:
    """Authenticate, and carry the client's identity into the handler."""

    require_enabled()

    identity = auth.identify(request.headers, request.query_params)

    if not auth.is_valid_token(identity.token):
        raise HTTPException(status_code=401, detail="Invalid token")

    return identity


Identity = Annotated[auth.ClientIdentity, Depends(require_auth)]


# --------------------------------------------------------------------------
# System / identity. Reachable unauthenticated: a client has to be able to ask
# "what are you" before it has any credentials to offer.
# --------------------------------------------------------------------------


def _public_info() -> dict[str, Any]:
    settings = settings_manager.settings.jellyfin_server

    return {
        "LocalAddress": settings.advertised_url or "",
        "ServerName": settings.server_name,
        "Version": ADVERTISED_VERSION,
        "ProductName": "Jellyfin Server",
        "OperatingSystem": "Linux",
        "Id": ids.SERVER_ID.replace("-", ""),
        "StartupWizardCompleted": True,
    }


@router.get("/System/Info/Public")
def system_info_public() -> dict[str, Any]:
    require_enabled()

    return _public_info()


@router.get("/System/Info")
def system_info(identity: Identity) -> dict[str, Any]:
    info = _public_info()
    info.update(
        {
            "HasUpdateAvailable": False,
            "SupportsLibraryMonitor": False,
            "WebSocketPortNumber": 8080,
            "CompletedInstallations": [],
            "CanSelfRestart": False,
            "CanLaunchWebBrowser": False,
        }
    )

    return info


@router.get("/System/Endpoint")
def system_endpoint(identity: Identity) -> dict[str, Any]:
    return {"IsLocal": True, "IsInNetwork": True}


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------


def _user_dto() -> dict[str, Any]:
    settings = settings_manager.settings.jellyfin_server

    return {
        "Name": settings.username,
        "ServerId": ids.SERVER_ID.replace("-", ""),
        "Id": ids.USER_ID.replace("-", ""),
        "HasPassword": True,
        "HasConfiguredPassword": True,
        "HasConfiguredEasyPassword": False,
        "EnableAutoLogin": False,
        "Configuration": {
            "PlayDefaultAudioTrack": True,
            "DisplayMissingEpisodes": False,
            "SubtitleMode": "Default",
            "EnableNextEpisodeAutoPlay": False,
        },
        "Policy": {
            "IsAdministrator": True,
            "IsHidden": False,
            "IsDisabled": False,
            "EnableMediaPlayback": True,
            "EnableAudioPlaybackTranscoding": True,
            "EnableVideoPlaybackTranscoding": True,
            "EnablePlaybackRemuxing": True,
            "EnableContentDeletion": False,
            "EnableContentDownloading": True,
            "EnableRemoteAccess": True,
            "EnableAllFolders": True,
            "EnabledFolders": [],
            "BlockedTags": [],
            "AccessSchedules": [],
        },
    }


@router.post("/Users/AuthenticateByName")
async def authenticate_by_name(request: Request) -> dict[str, Any]:
    require_enabled()

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed body")

    username = body.get("Username") or body.get("username") or ""
    # Jellyfin sends the password as "Pw"; "Password" is the older field and
    # some clients still send it.
    password = body.get("Pw") or body.get("Password") or ""

    identity = auth.identify(request.headers, request.query_params)

    if not auth.check_password(username, password):
        logger.warning(
            f"Jellyfin auth rejected for {username!r} from {identity.label}"
        )
        # Jellyfin answers 401 here; clients show "invalid username or
        # password" rather than a network error.
        raise HTTPException(status_code=401, detail="Invalid username or password")

    logger.info(f"Jellyfin client authenticated: {identity.label}")

    return {
        "User": _user_dto(),
        "SessionInfo": {
            "Id": ids.USER_ID.replace("-", ""),
            "UserId": ids.USER_ID.replace("-", ""),
            "UserName": settings_manager.settings.jellyfin_server.username,
            "Client": identity.client or "",
            "DeviceName": identity.device or "",
            "DeviceId": identity.device_id or "",
            "ApplicationVersion": identity.version or "",
            "SupportsRemoteControl": False,
        },
        "AccessToken": auth.issue_token(),
        "ServerId": ids.SERVER_ID.replace("-", ""),
    }


@router.get("/Users/Me")
def users_me(identity: Identity) -> dict[str, Any]:
    return _user_dto()


@router.get("/Users/Public")
def users_public() -> list[dict[str, Any]]:
    require_enabled()

    # Deliberately empty. Listing the user here would let a client offer a
    # tap-to-log-in tile, which is exactly the wrong affordance for a server
    # whose password is an API key.
    return []


@router.get("/Users/{user_id}")
def users_get(user_id: str, identity: Identity) -> dict[str, Any]:
    return _user_dto()


# --------------------------------------------------------------------------
# Library structure
# --------------------------------------------------------------------------


def _library_dto() -> dict[str, Any]:
    settings = settings_manager.settings.jellyfin_server

    return {
        "Id": ids.LIBRARY_ID.replace("-", ""),
        "ServerId": ids.SERVER_ID.replace("-", ""),
        "Name": settings.library_name,
        "Type": "CollectionFolder",
        "CollectionType": "movies",
        "IsFolder": True,
        "MediaType": "Unknown",
        "ImageTags": {},
        "BackdropImageTags": [],
        "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "Played": False},
    }


@router.get("/Users/{user_id}/Views")
def user_views(user_id: str, identity: Identity) -> dict[str, Any]:
    return {"Items": [_library_dto()], "TotalRecordCount": 1, "StartIndex": 0}


@router.get("/UserViews")
def user_views_alias(identity: Identity) -> dict[str, Any]:
    return {"Items": [_library_dto()], "TotalRecordCount": 1, "StartIndex": 0}


@router.get("/Library/VirtualFolders")
def virtual_folders(identity: Identity) -> list[dict[str, Any]]:
    settings = settings_manager.settings.jellyfin_server

    return [
        {
            "Name": settings.library_name,
            "ItemId": ids.LIBRARY_ID.replace("-", ""),
            "CollectionType": "movies",
            "Locations": [],
            "LibraryOptions": {},
        }
    ]


# --------------------------------------------------------------------------
# Browsing
# --------------------------------------------------------------------------


def _playable_query():
    """Only items with something to play.

    A Jellyfin client has no state between "in the library" and "playable" --
    an item it can see is one it expects to start. Showing requested-but-not-
    yet-downloaded titles would make most of the grid dead ends.
    """

    from program.media.filesystem_entry import FilesystemEntry

    return (
        select(MediaItem)
        .where(MediaItem.type.in_(("movie", "episode")))
        .where(
            select(FilesystemEntry.id)
            .where(FilesystemEntry.media_item_id == MediaItem.id)
            .exists()
        )
    )


def _apply_sort(query, sort_by: str | None, sort_order: str | None):
    columns = {
        "sortname": MediaItem.title,
        "name": MediaItem.title,
        "premieredate": MediaItem.aired_at,
        "productionyear": MediaItem.year,
        "communityrating": MediaItem.rating,
        "datecreated": MediaItem.requested_at,
        "random": func.random(),
    }

    key = (sort_by or "").split(",")[0].strip().lower()
    column = columns.get(key, MediaItem.title)

    descending = (sort_order or "").strip().lower() == "descending"

    return query.order_by(column.desc() if descending else column.asc())


@router.get("/Items")
@router.get("/Users/{user_id}/Items")
def get_items(
    identity: Identity,
    user_id: str | None = None,
    searchTerm: Annotated[str | None, Query()] = None,
    startIndex: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    sortBy: Annotated[str | None, Query()] = None,
    sortOrder: Annotated[str | None, Query()] = None,
    ids_: Annotated[str | None, Query(alias="Ids")] = None,
    parentId: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """The library grid, search results, and 'resolve these ids' in one route.

    Jellyfin overloads this endpoint heavily; the parameters implemented here
    are the ones clients were seen to send. Unknown parameters are ignored
    rather than rejected, because a client sending a filter we do not honour
    should get a broader result set, not an error screen.
    """

    with db_session() as session:
        query = _playable_query()

        if ids_:
            wanted = [
                parsed
                for raw in ids_.split(",")
                if (parsed := ids.from_guid(raw.strip())) is not None
            ]

            if not wanted:
                return {"Items": [], "TotalRecordCount": 0, "StartIndex": startIndex}

            query = query.where(MediaItem.id.in_(wanted))

        if searchTerm:
            pattern = f"%{searchTerm.strip()}%"
            # Performers are a JSON array, so it is matched as text. Crude, but
            # it means searching a performer's name finds their scenes, which
            # is how this library is actually browsed.
            query = query.where(
                or_(
                    MediaItem.title.ilike(pattern),
                    func.cast(MediaItem.performers, sqlalchemy.Text).ilike(pattern),
                )
            )

        total = session.execute(
            select(func.count()).select_from(query.subquery())
        ).scalar_one()

        rows = (
            session.execute(
                _apply_sort(query, sortBy, sortOrder).offset(startIndex).limit(limit)
            )
            .scalars()
            .all()
        )

        return {
            "Items": [mapping.base_item(item) for item in rows],
            "TotalRecordCount": total,
            "StartIndex": startIndex,
        }



@router.get("/Items/{item_id}")
@router.get("/Users/{user_id}/Items/{item_id}")
def get_item(item_id: str, identity: Identity, user_id: str | None = None) -> dict[str, Any]:
    parsed = ids.from_guid(item_id)

    if parsed is None:
        raise HTTPException(status_code=404, detail="Not found")

    with db_session() as session:
        item = session.get(MediaItem, parsed)

        if item is None:
            raise HTTPException(status_code=404, detail="Not found")

        return mapping.base_item(item, include_media=True)


@router.get("/Users/{user_id}/Items/Latest")
def items_latest(
    user_id: str,
    identity: Identity,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[dict[str, Any]]:
    with db_session() as session:
        rows = (
            session.execute(
                _playable_query()
                .order_by(MediaItem.requested_at.desc().nullslast())
                .limit(limit)
            )
            .scalars()
            .all()
        )

        return [mapping.base_item(item) for item in rows]


@router.get("/Items/{item_id}/Images/{image_type}")
def item_image(item_id: str, image_type: str) -> Response:
    """Redirect to the poster rather than proxying it.

    These are TPDB CDN URLs, and a redirect keeps hundreds of grid thumbnails
    off this server entirely. Unauthenticated on purpose: clients load images
    from plain <img> tags that carry no token, and the URL discloses nothing
    that the item list did not already.
    """

    require_enabled()

    parsed = ids.from_guid(item_id)

    if parsed is None:
        raise HTTPException(status_code=404, detail="Not found")

    with db_session() as session:
        item = session.get(MediaItem, parsed)

        if item is None or not item.poster_path:
            raise HTTPException(status_code=404, detail="No image")

        return RedirectResponse(item.poster_path, status_code=302)


# --------------------------------------------------------------------------
# Playback
# --------------------------------------------------------------------------


def _probe_from_stored(metadata) -> transcode.MediaProbe | None:
    """Build a probe result out of what the database already knows.

    The point of this function is that it does no I/O. `MediaAnalysisService`
    probed the file once, at download time, and wrote codecs and container
    into `media_metadata`; re-probing here would open the debrid URL on every
    details screen and every playback attempt. Returns None when the item was
    never analysed, and the caller falls back to a real probe.
    """

    if not metadata:
        return None

    video = getattr(metadata, "video", None)
    audio_tracks = getattr(metadata, "audio_tracks", None) or []
    containers = getattr(metadata, "container_formats", None) or []

    if not video or not video.codec:
        return None

    return transcode.MediaProbe(
        duration=getattr(metadata, "duration", None) or 0.0,
        video_codec=video.codec,
        audio_codec=audio_tracks[0].codec if audio_tracks else None,
        container=containers[0] if containers else None,
        width=video.resolution_width,
        height=video.resolution_height,
    )


@router.post("/Items/{item_id}/PlaybackInfo")
@router.get("/Items/{item_id}/PlaybackInfo")
async def playback_info(item_id: str, request: Request, identity: Identity) -> dict[str, Any]:
    """Decide how this particular client should receive this particular file.

    This is where the masquerade earns its keep. The client posts a
    DeviceProfile describing what it can decode; the decision is made against
    that profile and the file's real codecs, rather than against a hardcoded
    idea of what "a client" supports. See `streaming/transcode.decide`.
    """

    parsed = ids.from_guid(item_id)

    if parsed is None:
        raise HTTPException(status_code=404, detail="Not found")

    profile: dict[str, Any] | None = None

    if request.method == "POST":
        try:
            body = await request.json()
            profile = (body or {}).get("DeviceProfile")
        except Exception:
            # A malformed profile is not worth a failed playback; falling
            # through means browser capabilities, which is conservative.
            profile = None

    caps = capabilities.from_device_profile(profile)

    with db_session() as session:
        item = session.get(MediaItem, parsed)

        if item is None:
            raise HTTPException(status_code=404, detail="Not found")

        entry = mapping._media_entry(item)
        metadata = getattr(entry, "media_metadata", None) if entry else None
        source = mapping.media_source(item, entry, metadata)

        probe = _probe_from_stored(metadata)

        if probe is None:
            # Never analysed. Assume it plays and let the client tell us
            # otherwise, rather than reading the file to find out -- see the
            # docstring on _probe_from_stored.
            mode, reason = "direct", "no stored analysis; offering direct stream"
        else:
            mode, reason = transcode.decide(probe, caps)

        logger.debug(
            f"Jellyfin PlaybackInfo item {parsed} for {identity.label}: "
            f"{mode} ({reason})"
        )

        token = auth.issue_token()

        if mode == "direct":
            source["SupportsDirectStream"] = True
            source["TranscodingUrl"] = None
        else:
            source["SupportsDirectStream"] = False
            source["TranscodingUrl"] = (
                f"/Videos/{item_id}/main.m3u8?api_key={token}&MediaSourceId={item_id}"
            )
            source["TranscodingSubProtocol"] = "hls"
            source["TranscodingContainer"] = "ts"

        return {
            "MediaSources": [source],
            "PlaySessionId": f"{parsed}",
        }


@router.get("/Videos/{item_id}/stream")
@router.get("/Videos/{item_id}/stream.{container}")
async def video_stream(item_id: str, request: Request, container: str | None = None):
    """Direct stream, delegated to the existing file-streaming path.

    Not reimplemented here: `routers/secure/stream.py` already handles link
    expiry with a single re-mint, Range in both directions, and the MIME
    correction some providers need. This endpoint exists to give that
    behaviour a Jellyfin-shaped URL.

    Authenticated from the query string as well as headers -- Jellyfin clients
    routinely hand a bare URL to a platform video player that sends no custom
    headers at all.
    """

    require_enabled()

    identity = auth.identify(request.headers, request.query_params)

    if not auth.is_valid_token(identity.token):
        raise HTTPException(status_code=401, detail="Invalid token")

    parsed = ids.from_guid(item_id)

    if parsed is None:
        raise HTTPException(status_code=404, detail="Not found")

    from routers.secure.stream import stream_file

    return await stream_file(parsed, request)


@router.get("/Videos/{item_id}/main.m3u8")
async def video_hls_playlist(item_id: str, request: Request):
    """HLS playlist, delegated to the existing transcoding session manager."""

    require_enabled()

    identity = auth.identify(request.headers, request.query_params)

    if not auth.is_valid_token(identity.token):
        raise HTTPException(status_code=401, detail="Invalid token")

    parsed = ids.from_guid(item_id)

    if parsed is None:
        raise HTTPException(status_code=404, detail="Not found")

    from routers.secure.stream import get_hls_playlist

    return await get_hls_playlist(parsed)


@router.get("/Videos/{item_id}/hls1/{playlist}/{seq}.ts")
@router.get("/Videos/{item_id}/segment/{seq}.ts")
async def video_hls_segment(
    item_id: str, seq: int, request: Request, playlist: str | None = None
):
    require_enabled()

    identity = auth.identify(request.headers, request.query_params)

    if not auth.is_valid_token(identity.token):
        raise HTTPException(status_code=401, detail="Invalid token")

    parsed = ids.from_guid(item_id)

    if parsed is None:
        raise HTTPException(status_code=404, detail="Not found")

    from routers.secure.stream import get_hls_segment

    return await get_hls_segment(parsed, seq)


# --------------------------------------------------------------------------
# Session reporting
#
# Accepted and discarded. Clients post progress unprompted and treat a 404 as
# an error worth showing the user, so these have to exist; but resume points
# are not implemented yet, and storing progress we do not read would be a
# second source of truth for watch state with nothing consuming it.
# --------------------------------------------------------------------------


@router.post("/Sessions/Playing")
@router.post("/Sessions/Playing/Progress")
@router.post("/Sessions/Playing/Stopped")
@router.post("/Sessions/Capabilities")
@router.post("/Sessions/Capabilities/Full")
async def sessions_sink(request: Request) -> Response:
    require_enabled()

    return Response(status_code=204)


@router.get("/Sessions")
def sessions_list(identity: Identity) -> list[dict[str, Any]]:
    return []


@router.get("/DisplayPreferences/{preference_id}")
def display_preferences(preference_id: str, identity: Identity) -> dict[str, Any]:
    """Clients fetch this before rendering a library and error if it 404s."""

    return {
        "Id": preference_id,
        "SortBy": "SortName",
        "SortOrder": "Ascending",
        "ViewType": "Poster",
        "Client": "riven",
        "RememberIndexing": False,
        "RememberSorting": False,
        "PrimaryImageHeight": 250,
        "PrimaryImageWidth": 250,
        "CustomPrefs": {},
    }
