"""Tests for the Jellyfin-compatible server surface.

Structured around the things that break silently. A masquerade fails in a
particularly unhelpful way -- the client shows an empty library or a black
screen rather than an error -- so the cases here are mostly "we emitted a
shape the client will quietly reject".

Stdlib only, and split in two: everything that does not need `program.settings`
(and therefore RTN and the DB models) runs anywhere, while the rest is skipped
with a notice when those are absent. Run the full set inside the container.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS = FAIL = 0


def check(name, condition, extra=""):
    global PASS, FAIL

    if condition:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f"  -- {extra}" if extra else ""))


# ---------------------------------------------------------------- identifiers

from program.services.jellyfin_server import ids  # noqa: E402

print("item id encoding")
check("an id survives the round trip", ids.from_guid(ids.to_guid(4242)) == 4242)
check("ids are the 32-hex shape clients expect", len(ids.to_guid(1)) == 32)
check(
    "a dashed guid is accepted",
    ids.from_guid("00000000-0000-0000-0000-000000000f00") == 0xF00,
)
check("garbage is rejected, not raised", ids.from_guid("not-a-guid") is None)
check("an empty id is rejected", ids.from_guid("") is None)
check("a zero id is rejected", ids.from_guid(ids.to_guid(0)) is None)

person = ids.synthetic_guid("person", "Angela White")
check("a synthetic id is stable across calls", person == ids.synthetic_guid("person", "Angela White"))
check("synthetic ids are case-insensitive", person == ids.synthetic_guid("person", "angela white"))
check("different people get different ids", person != ids.synthetic_guid("person", "Riley Reid"))
check(
    "a person and a studio of the same name do not collide",
    ids.synthetic_guid("person", "Brazzers") != ids.synthetic_guid("studio", "Brazzers"),
)
# The important one: a synthetic id must never be mistaken for a real item.
check("a synthetic id is not read back as an item id", ids.from_guid(person) is None)
check("synthetic ids are the right shape too", len(person) == 32)

# --------------------------------------------------------------- auth parsing

from program.services.jellyfin_server import auth  # noqa: E402

print("\nauthorization header parsing")
modern = (
    'MediaBrowser Token="abc123", Client="Android TV", Device="Shield", '
    'DeviceId="zq9", Version="0.15.3"'
)
identity = auth.parse_authorization(modern)
check("token is read from the MediaBrowser scheme", identity.token == "abc123")
check("client is carried through", identity.client == "Android TV")
check("device id is carried through", identity.device_id == "zq9")
check("a label is produced for logging", "Android TV" in identity.label)

check("an absent header yields an empty identity", auth.parse_authorization(None).token is None)
check("an unparseable header does not raise", auth.parse_authorization("garbage").token is None)
check(
    "a value containing a comma is not split",
    auth.parse_authorization('MediaBrowser Client="Foo, Bar", Token="t"').client == "Foo, Bar",
)


class _Headers(dict):
    def get(self, key, default=None):
        return dict.get(self, key.lower(), default)


print("\nevery header form a client might use")
check(
    "modern Authorization header",
    auth.identify(_Headers({"authorization": modern}), {}).token == "abc123",
)
check(
    "legacy X-Emby-Authorization",
    auth.identify(_Headers({"x-emby-authorization": modern}), {}).token == "abc123",
)
check(
    "bare X-Emby-Token",
    auth.identify(_Headers({"x-emby-token": "t2"}), {}).token == "t2",
)
check(
    "bare X-MediaBrowser-Token",
    auth.identify(_Headers({"x-mediabrowser-token": "t3"}), {}).token == "t3",
)
check("ApiKey query parameter", auth.identify(_Headers(), {"ApiKey": "t4"}).token == "t4")
check("api_key query parameter", auth.identify(_Headers(), {"api_key": "t5"}).token == "t5")
check("no credentials at all", auth.identify(_Headers(), {}).token is None)
# A client sends the device metadata in the MediaBrowser header and the token
# separately; losing the metadata makes "why is this transcoding" unanswerable.
merged = auth.identify(
    _Headers({"authorization": 'MediaBrowser Client="Roku"', "x-emby-token": "t6"}), {}
)
check("device metadata survives a bare-token fallback", merged.client == "Roku" and merged.token == "t6")

# --------------------------------------------------------------- capabilities

# `program.services.streaming.__init__` pulls in trio and the debrid clients,
# none of which the playback DECISION depends on. Load `transcode` directly
# and stand in for its one intra-package import, the same way test_vpn.py
# isolates the VPN provider seam.
import importlib.util  # noqa: E402
import types  # noqa: E402

if "program.services.streaming" not in sys.modules:
    package = types.ModuleType("program.services.streaming")
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "program/services/streaming")]
    sys.modules["program.services.streaming"] = package

    stub = types.ModuleType("program.services.streaming.playback_url")
    stub.redact = lambda value: value
    sys.modules["program.services.streaming.playback_url"] = stub

    spec = importlib.util.spec_from_file_location(
        "program.services.streaming.transcode",
        Path(__file__).resolve().parents[1] / "program/services/streaming/transcode.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["program.services.streaming.transcode"] = module
    spec.loader.exec_module(module)

from program.services.jellyfin_server import capabilities  # noqa: E402
from program.services.streaming.transcode import BROWSER, Capabilities, MediaProbe, decide  # noqa: E402

print("\ndevice profile -> capabilities")
profile = {
    "DirectPlayProfiles": [
        {"Container": "mp4,mkv", "Type": "Video", "VideoCodec": "h264,hevc", "AudioCodec": "aac,ac3"},
        {"Container": "mp3", "Type": "Audio", "AudioCodec": "mp3"},
    ]
}
caps = capabilities.from_device_profile(profile)
check("video codecs are collected", caps.video_codecs == frozenset({"h264", "hevc"}))
check("containers are collected", caps.containers == frozenset({"mp4", "mkv"}))
check("audio codecs are collected", caps.audio_codecs == frozenset({"aac", "ac3"}))
check(
    "an Audio profile does not contribute video capability",
    "mp3" not in caps.containers,
)
check("a missing profile falls back to browser", capabilities.from_device_profile(None) is BROWSER)
check("an empty profile falls back to browser", capabilities.from_device_profile({}) is BROWSER)
check(
    "a profile with no direct-play entries falls back to browser",
    capabilities.from_device_profile({"DirectPlayProfiles": []}) is BROWSER,
)
check(
    "a list-valued container is handled",
    "mp4" in capabilities.from_device_profile(
        {"DirectPlayProfiles": [{"Container": ["mp4"], "VideoCodec": ["h264"]}]}
    ).containers,
)

print("\nplayback decisions are per-client, not hardcoded")
hevc = MediaProbe(video_codec="hevc", audio_codec="aac", container="mp4")
h264 = MediaProbe(video_codec="h264", audio_codec="aac", container="mp4")

# The whole point of the refactor: the same file, two clients, two answers.
check("a browser transcodes HEVC", decide(hevc, BROWSER)[0] == "transcode")
check("an HEVC-capable client direct-plays it", decide(hevc, caps)[0] == "direct")
check("both agree on plain h264/aac", decide(h264, BROWSER)[0] == decide(h264, caps)[0] == "direct")

# Existing callers pass no capabilities and must keep the old behaviour.
check("the default is still the browser", decide(hevc)[0] == decide(hevc, BROWSER)[0])
check(
    "an unprobed file is still assumed playable",
    decide(MediaProbe())[0] == "direct",
)
roku = Capabilities(
    video_codecs=frozenset({"h264"}), audio_codecs=frozenset({"aac"}), containers=frozenset({"mp4"})
)
check(
    "a container the client cannot open is remuxed, not transcoded",
    decide(MediaProbe(video_codec="h264", audio_codec="aac", container="mkv"), roku)[0] == "remux",
)
check(
    "an audio codec the client cannot decode is remuxed",
    decide(MediaProbe(video_codec="h264", audio_codec="truehd", container="mp4"), roku)[0] == "remux",
)

# ------------------------------------------------------------------- mapping

from program.services.jellyfin_server import mapping  # noqa: E402

print("\nlibrary item -> BaseItemDto")


class _Video:
    codec = "h264"
    resolution_width = 1920
    resolution_height = 1080
    frame_rate = 23.976
    bit_depth = 8
    hdr_type = None
    resolution_label = "1080p"


class _Audio:
    codec = "aac"
    channels = 6
    sample_rate = 48000
    language = "eng"


class _Metadata:
    video = _Video()
    audio_tracks = [_Audio()]
    subtitle_tracks = []
    duration = 1800.0
    file_size = 123456789
    bitrate = 5_000_000
    container_formats = ["mp4"]


class _Entry:
    entry_type = "media"
    is_directory = False
    file_size = 123456789
    media_metadata = _Metadata()


class _Item:
    id = 7
    title = "Test Scene"
    type = "movie"
    year = 2024
    rating = 8.5
    content_rating = "XXX"
    genres = ["Feature"]
    performers = ["Angela White", "Mick Blue"]
    network = "Brazzers"
    site_name = "Brazzers University"
    tpdb_id = "abc-123"
    imdb_id = None
    tmdb_id = None
    aired_at = None
    poster_path = "https://cdn.example/poster.jpg"
    requested_at = None
    filesystem_entries = [_Entry()]


dto = mapping.base_item(_Item())
check("id is the encoded item id", dto["Id"] == ids.to_guid(7))
check("a scene is a Movie", dto["Type"] == "Movie" and dto["MediaType"] == "Video")
check("it is not a folder", dto["IsFolder"] is False)
check("performers become People", [p["Name"] for p in dto["People"]] == ["Angela White", "Mick Blue"])
check("people are typed as actors", all(p["Type"] == "Actor" for p in dto["People"]))
check("network and site both become studios", [s["Name"] for s in dto["Studios"]] == ["Brazzers", "Brazzers University"])
check("tpdb id is exposed as a provider id", dto["ProviderIds"] == {"Tpdb": "abc-123"})
# Ticks are the single most common way to get this wrong; seconds render as a
# film lasting a fraction of a second and clients skip straight to the end.
check("runtime is in 100ns ticks", dto["RunTimeTicks"] == 18_000_000_000, dto["RunTimeTicks"])
check("an image tag is set when there is a poster", bool(dto["ImageTags"].get("Primary")))
check("UserData is present", "PlaybackPositionTicks" in dto["UserData"])
check("the grid payload omits MediaSources", "MediaSources" not in dto)

detail = mapping.base_item(_Item(), include_media=True)
check("the detail payload includes MediaSources", len(detail["MediaSources"]) == 1)

print("\nmedia source and streams")
source = detail["MediaSources"][0]
check("container comes from stored metadata", source["Container"] == "mp4")
check("size comes from the filesystem entry", source["Size"] == 123456789)
check("direct stream is supported", source["SupportsDirectStream"] is True)
# The client cannot reach a path inside our FUSE mount, so claiming direct
# play makes it try to open a file that does not exist on its machine.
check("direct play is NOT claimed", source["SupportsDirectPlay"] is False)

streams = source["MediaStreams"]
check("a video stream is present", streams[0]["Type"] == "Video")
check("video dimensions are carried", (streams[0]["Width"], streams[0]["Height"]) == (1920, 1080))
check("an audio stream follows", streams[1]["Type"] == "Audio")
check("channel layout is described", "5.1" in streams[1]["DisplayTitle"])
check("stream indexes are unique", len({s["Index"] for s in streams}) == len(streams))


class _Bare:
    id = 9
    title = None
    year = None
    rating = None
    content_rating = None
    genres = None
    performers = None
    network = None
    site_name = None
    tpdb_id = None
    imdb_id = None
    tmdb_id = None
    aired_at = None
    poster_path = None
    requested_at = None
    filesystem_entries = []


bare = mapping.base_item(_Bare(), include_media=True)
check("an item with no metadata still produces a name", bare["Name"] == "Untitled")
check("no poster means no image tag", bare["ImageTags"] == {})
check("no analysis means no runtime rather than zero", bare["RunTimeTicks"] is None)
check("an item with no file still yields a source", len(bare["MediaSources"]) == 1)
check("empty metadata yields no streams", bare["MediaSources"][0]["MediaStreams"] == [])

# ------------------------------------------------- settings-dependent section

print("\nauth against real settings")

try:
    from program.settings import settings_manager
except ModuleNotFoundError as exc:
    print(f"SKIP: {exc} (run inside the container for this section)")
else:
    from unittest.mock import patch

    class _JF:
        enabled = True
        username = "riven"
        server_name = "Riven"
        discovery = False
        library_name = "Library"
        advertised_url = ""

    class _Settings:
        api_key = "secret-key"
        jellyfin_server = _JF()

    class _Manager:
        settings = _Settings()

    with patch.object(settings_manager, "settings", _Settings()):
        check("the right password authenticates", auth.check_password("riven", "secret-key"))
        check("the username is case-insensitive", auth.check_password("RIVEN", "secret-key"))
        check("a wrong password is rejected", not auth.check_password("riven", "nope"))
        check("a wrong username is rejected", not auth.check_password("someone", "secret-key"))
        check("an empty password is rejected", not auth.check_password("riven", ""))
        check("the issued token is the api key", auth.issue_token() == "secret-key")
        check("the api key validates as a token", auth.is_valid_token("secret-key"))
        check("another token does not", not auth.is_valid_token("secret-key2"))
        check("an empty token does not", not auth.is_valid_token(""))
        check("a None token does not", not auth.is_valid_token(None))

    # With no API key configured, nothing may authenticate -- otherwise a
    # fresh install would serve the whole library to anyone who found the port.
    class _NoKey:
        api_key = ""
        jellyfin_server = _JF()

    with patch.object(settings_manager, "settings", _NoKey()):
        check("an unconfigured server rejects everything", not auth.is_valid_token(""))
        check("an unconfigured server rejects any password", not auth.check_password("riven", ""))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
