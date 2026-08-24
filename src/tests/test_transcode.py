"""Tests for playback-mode selection, playlist generation and URL resolution.

The cases mirror what is actually in this library: TorBox entries whose stored
URL is either an expired CDN link or the internal `torbox://` reference, and
H.264/AAC MP4 files that the old player sent through a full x264 re-encode
whenever the viewer was on Firefox.
"""

from program.services.streaming.playback_url import is_playable, redact
from program.services.streaming.transcode import (
    SEGMENT_DURATION,
    MediaProbe,
    Session,
    build_playlist,
    build_remux_command,
    decide,
)

PASS = FAIL = 0


def check(name, condition, extra=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def probe(video=None, audio=None, container="mp4", duration=600.0):
    return MediaProbe(
        video_codec=video, audio_codec=audio, container=container, duration=duration
    )


print("\n-- url classification --")
check("https is playable", is_playable("https://cdn.example/dld/abc?token=x"))
check("http is playable", is_playable("http://cdn.example/f.mp4"))
check(
    "torbox reference is not playable",
    not is_playable("torbox://82403543/1"),
    "this exact value was being handed to ffmpeg, which reported 'Protocol not found'",
)
check("magnet is not playable", not is_playable("magnet:?xt=urn:btih:abc"))
check("None is not playable", not is_playable(None))
check("empty is not playable", not is_playable(""))

print("\n-- token redaction --")
check(
    "token query param is redacted",
    redact("https://cdn.tb-cdn.st/dld/abc?token=SECRETVALUE")
    == "https://cdn.tb-cdn.st/dld/abc?token=REDACTED",
)
check(
    "api_key is redacted",
    "SECRET" not in redact("https://x/y?foo=1&api_key=SECRET"),
)
check(
    "redaction keeps the rest of the url legible",
    redact("https://cdn/x?token=S&file=9").startswith("https://cdn/x?token=REDACTED"),
)
check("None redacts to a placeholder", redact(None) == "<none>")

print("\n-- redaction of provider errors --")
# httpx embeds the failing URL in its exception message, so the token leaks
# through error paths even when the URL itself is logged redacted.
_httpx_message = (
    "Client error '400 Bad Request' for url "
    "'https://nexus-070.ceur.tb-cdn.st/dld/f323d61f?token=7b0e6d30-634f-46ba'"
)
check(
    "a token inside an httpx error message is redacted",
    "7b0e6d30" not in redact(_httpx_message),
)
check(
    "the status code survives redaction",
    "400 Bad Request" in redact(_httpx_message),
)

print("\n-- playback mode: the regression that caused this work --")
check(
    "h264/aac mp4 plays directly",
    decide(probe("h264", "aac", "mp4"))[0] == "direct",
    "Firefox was transcoding these because the old probe asked about HEVC support",
)
check(
    "hevc is transcoded",
    decide(probe("hevc", "aac", "mp4"))[0] == "transcode",
)
check(
    "h264 with ac3 audio is remuxed, not re-encoded",
    decide(probe("h264", "ac3", "mp4"))[0] == "remux",
)
check(
    "h264 with eac3 audio is remuxed",
    decide(probe("h264", "eac3", "mkv"))[0] == "remux",
)
check(
    "h264/aac in mkv is remuxed for the container alone",
    decide(probe("h264", "aac", "matroska"))[0] == "remux",
)
check(
    "vp9/opus webm plays directly",
    decide(probe("vp9", "opus", "webm"))[0] == "direct",
)
check(
    "av1 plays directly",
    decide(probe("av1", "opus", "mp4"))[0] == "direct",
)
check(
    "a video with no audio track still plays directly",
    decide(probe("h264", None, "mp4"))[0] == "direct",
)
check(
    "mpeg2 video is transcoded",
    decide(probe("mpeg2video", "mp2", "mpegts"))[0] == "transcode",
)
check(
    "a failed probe falls back to direct rather than burning CPU",
    decide(MediaProbe())[0] == "direct",
)
check(
    "every decision carries a reason",
    all(decide(p)[1] for p in [probe("h264", "aac"), probe("hevc", "aac"), MediaProbe()]),
)

print("\n-- playlist --")
playlist = build_playlist(60.0)
check("playlist starts with the tag", playlist.startswith("#EXTM3U"))
check("playlist ends the list", playlist.strip().endswith("#EXT-X-ENDLIST"))
check("playlist is VOD", "#EXT-X-PLAYLIST-TYPE:VOD" in playlist)
check(
    "60s at 6s per segment is 10 segments",
    playlist.count("segment/") == 10,
    playlist.count("segment/"),
)
check("segments are numbered from zero", "segment/0.ts" in playlist)
check("last segment index is 9", "segment/9.ts" in playlist and "segment/10.ts" not in playlist)

ragged = build_playlist(65.0)
check(
    "a ragged duration rounds up to cover the tail",
    ragged.count("segment/") == 11,
    ragged.count("segment/"),
)
check(
    "the final segment advertises only its real length",
    "#EXTINF:5.000000," in ragged,
    "otherwise players seek past the end of the file",
)

unknown = build_playlist(0.0)
check(
    "an unprobeable file still gets a playable stub",
    unknown.count("segment/") == 10,
)
check("negative duration is treated as unknown", build_playlist(-5.0).count("segment/") == 10)

print("\n-- session command --")


def session(copy_video=False, copy_audio=False, start=0):
    from pathlib import Path

    return Session(
        item_id=1,
        url="https://cdn/x.mp4",
        start_seq=start,
        copy_video=copy_video,
        copy_audio=copy_audio,
        directory=Path("/tmp/riven-test"),
    )


cmd = session().build_command()
check("forces keyframes on segment boundaries", "-force_key_frames" in cmd,
      "without this the segments drift from the durations in the playlist")
check(
    "keyframe expression matches the segment length",
    f"expr:gte(t,n_forced*{SEGMENT_DURATION})" in cmd,
)
check("re-encodes with libx264 by default", "libx264" in cmd)
check("encodes audio to aac when it is not already", "aac" in cmd)
check("reconnects on a dropped CDN connection", "-reconnect" in cmd)
check("writes an hls playlist", "hls" in cmd)

copy_cmd = session(copy_audio=True).build_command()
check(
    "aac audio is copied rather than re-encoded",
    "copy" in copy_cmd and "aac" not in copy_cmd,
)

seek_cmd = session(start=10).build_command()
check(
    "seeking passes -ss before -i so ffmpeg can range-request",
    seek_cmd.index("-ss") < seek_cmd.index("-i"),
    "-ss after -i decodes the whole file up to the seek point",
)
check(
    "seek offset is the segment index times its duration",
    str(10 * SEGMENT_DURATION) in seek_cmd,
)
check(
    "segment numbering starts at the seek point",
    seek_cmd[seek_cmd.index("-start_number") + 1] == "10",
    "otherwise segment filenames stop matching the playlist indices",
)
check("segment zero omits -ss entirely", "-ss" not in session(start=0).build_command())

print("\n-- session coverage --")
s = session(start=10)
check("a session covers its own start", s.covers(10))
check("a session covers later segments", s.covers(50))
check(
    "a session does not cover a backwards seek",
    not s.covers(3),
    "restarting is the only way to serve an earlier segment",
)

print("\n-- remux command --")
remux = build_remux_command("https://cdn/x.mkv")
check("remux copies the video stream", "copy" in remux)
check("remux does not invoke x264", "libx264" not in remux)
check("remux converts audio to aac", "aac" in remux)
check("remux emits a fragmented mp4", "frag_keyframe+empty_moov+default_base_is_moof" in remux)
check("remux writes to stdout", remux[-1] == "-")
check("remux without a seek omits -ss", "-ss" not in build_remux_command("https://cdn/x.mkv"))
check(
    "remux with a seek places -ss before -i",
    (lambda c: c.index("-ss") < c.index("-i"))(build_remux_command("https://cdn/x.mkv", 90.0)),
)

print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
