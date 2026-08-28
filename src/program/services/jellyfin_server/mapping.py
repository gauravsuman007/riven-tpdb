"""Translate Riven's library into the shapes Jellyfin clients expect.

Plain dicts rather than pydantic models, deliberately. Jellyfin's schema is
enormous, clients are inconsistent about which fields they require, and the
cost of being wrong is a client that silently renders nothing. Dicts keep the
exact wire shape visible in one place and let a field be added because a real
client asked for it, which is how this surface should grow -- see AGENTS.md on
measuring rather than implementing the documented API.

The mapping itself is the API's contract, so it is stated once, here:

    scene            -> Movie          (not Series/Episode; see AGENTS.md)
    performers       -> People, Type "Actor"
    site_name/network-> Studios
    tpdb_id          -> ProviderIds
"""

from datetime import datetime, timezone
from typing import Any

from program.services.jellyfin_server import ids

#: Jellyfin measures time in 100-nanosecond ticks, everywhere.
TICKS_PER_SECOND = 10_000_000


def to_ticks(seconds: float | None) -> int | None:
    return int(seconds * TICKS_PER_SECOND) if seconds else None


def _iso(value: datetime | None) -> str | None:
    if not value:
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    return value.isoformat().replace("+00:00", "Z")


def _studio_names(item) -> list[str]:
    """Studio for a scene is the site it came from.

    `network` is the TPDB parent network and `site_name` the specific site;
    both are shown when they differ, because "Brazzers" and "Brazzers
    University" are genuinely different levels and a client browsing by studio
    wants the one it has.
    """

    names = []

    for value in (getattr(item, "network", None), getattr(item, "site_name", None)):
        cleaned = (value or "").strip()

        if cleaned and cleaned not in names:
            names.append(cleaned)

    return names


def _media_entry(item):
    """The filesystem entry actually worth playing, if any."""

    for entry in getattr(item, "filesystem_entries", None) or []:
        if getattr(entry, "entry_type", None) == "subtitle":
            continue

        if getattr(entry, "is_directory", False):
            continue

        return entry

    return None


def media_streams(metadata) -> list[dict[str, Any]]:
    """Per-track detail, read from stored metadata -- never by probing.

    This is the reason `PlaybackInfo` can be answered without touching the
    VFS: `MediaAnalysisService` already wrote codecs, dimensions and track
    languages into `media_metadata` when the file was downloaded. Probing here
    would mean reading the file, and reading the file means pulling chunks from
    the debrid provider on every client that opens a details page.
    """

    if not metadata:
        return []

    streams: list[dict[str, Any]] = []
    index = 0

    if video := getattr(metadata, "video", None):
        streams.append(
            {
                "Type": "Video",
                "Index": index,
                "Codec": video.codec,
                "Width": video.resolution_width,
                "Height": video.resolution_height,
                "AverageFrameRate": video.frame_rate,
                "RealFrameRate": video.frame_rate,
                "BitDepth": video.bit_depth,
                "VideoRange": "HDR" if video.hdr_type else "SDR",
                "VideoRangeType": video.hdr_type or "SDR",
                "DisplayTitle": video.resolution_label or video.codec or "Video",
                "IsDefault": True,
                "IsInterlaced": False,
            }
        )
        index += 1

    for track in getattr(metadata, "audio_tracks", None) or []:
        streams.append(
            {
                "Type": "Audio",
                "Index": index,
                "Codec": track.codec,
                "Channels": track.channels,
                "SampleRate": track.sample_rate,
                "Language": track.language,
                "DisplayTitle": " ".join(
                    part
                    for part in (track.language, track.codec, _channel_label(track.channels))
                    if part
                )
                or "Audio",
                "IsDefault": index == 1,
            }
        )
        index += 1

    for track in getattr(metadata, "subtitle_tracks", None) or []:
        streams.append(
            {
                "Type": "Subtitle",
                "Index": index,
                "Codec": track.codec,
                "Language": track.language,
                "DisplayTitle": track.language or "Subtitle",
                "IsDefault": False,
                "IsExternal": False,
            }
        )
        index += 1

    return streams


def _channel_label(channels: int | None) -> str | None:
    return {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}.get(channels or 0)


def base_item(item, *, include_media: bool = False) -> dict[str, Any]:
    """One library item as a Jellyfin BaseItemDto.

    `include_media` adds MediaSources, which the details screen needs and a
    grid of a thousand posters very much does not.
    """

    entry = _media_entry(item)
    metadata = getattr(entry, "media_metadata", None) if entry else None

    duration = getattr(metadata, "duration", None) if metadata else None

    dto: dict[str, Any] = {
        "Id": ids.to_guid(item.id),
        "ServerId": ids.SERVER_ID,
        "Name": item.title or "Untitled",
        "Type": "Movie",
        "MediaType": "Video",
        "IsFolder": False,
        "LocationType": "FileSystem",
        "ParentId": ids.LIBRARY_ID,
        "RunTimeTicks": to_ticks(duration),
        "ProductionYear": item.year,
        "PremiereDate": _iso(getattr(item, "aired_at", None)),
        "CommunityRating": item.rating,
        "OfficialRating": getattr(item, "content_rating", None),
        "Genres": list(item.genres or []),
        "Studios": [
            {"Name": name, "Id": ids.synthetic_guid("studio", name)}
            for name in _studio_names(item)
        ],
        "People": [
            {
                "Name": name,
                "Id": ids.synthetic_guid("person", name),
                "Type": "Actor",
                "Role": "",
            }
            for name in (item.performers or [])
        ],
        "ProviderIds": {
            key: value
            for key, value in (
                ("Tpdb", item.tpdb_id),
                ("Imdb", item.imdb_id),
                ("Tmdb", item.tmdb_id),
            )
            if value
        },
        # Clients cache images against this tag and will not re-fetch until it
        # changes, so it must be derived from something that moves when the
        # image does.
        "ImageTags": {"Primary": _image_tag(item)} if item.poster_path else {},
        "BackdropImageTags": [],
        "UserData": {
            "PlaybackPositionTicks": 0,
            "PlayCount": 0,
            "Played": False,
            "IsFavorite": False,
            "Key": str(item.id),
        },
    }

    if include_media:
        dto["MediaSources"] = [media_source(item, entry, metadata)]
        dto["MediaStreams"] = media_streams(metadata)

    return dto


def _image_tag(item) -> str:
    from hashlib import blake2b

    return blake2b((item.poster_path or "").encode(), digest_size=8).hexdigest()


def media_source(item, entry, metadata) -> dict[str, Any]:
    """One playable source.

    `SupportsDirectPlay` is false even when the file would play untouched: direct
    play means the CLIENT opens the path itself, and our path is inside a FUSE
    mount on the server that no client can reach. Direct STREAM is the
    equivalent here -- the client gets bytes from us without transcoding.
    """

    container = None

    if metadata and getattr(metadata, "container_formats", None):
        container = metadata.container_formats[0]

    streams = media_streams(metadata)

    return {
        "Id": ids.to_guid(item.id),
        "Protocol": "Http",
        "Type": "Default",
        "Name": item.title or "Untitled",
        "Container": container,
        "Size": getattr(entry, "file_size", None) if entry else None,
        "RunTimeTicks": to_ticks(getattr(metadata, "duration", None) if metadata else None),
        "Bitrate": getattr(metadata, "bitrate", None) if metadata else None,
        "IsRemote": False,
        "ReadAtNativeFramerate": False,
        "IgnoreDts": False,
        "IgnoreIndex": False,
        "GenPtsInput": False,
        "SupportsTranscoding": True,
        "SupportsDirectStream": True,
        "SupportsDirectPlay": False,
        "RequiresOpening": False,
        "RequiresClosing": False,
        "SupportsProbing": False,
        "MediaStreams": streams,
        "MediaAttachments": [],
        "Formats": [],
    }
