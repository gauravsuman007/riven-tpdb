"""
Playback-mode selection and the persistent HLS transcoding session.

Three problems with the inherited implementation are addressed here.

1. It picked its playback mode by asking the *browser* whether it could decode
   HEVC, and never looked at the file. Firefox supports no HEVC, so Firefox
   transcoded every file -- including plain H.264/AAC MP4s it plays natively.
   `probe()` reads the file's real codecs so the decision can be made against
   the actual stream (see `PlaybackInfo`, served to the client).

2. It re-encoded video unconditionally. When only the audio codec is
   unplayable, the video stream is now copied.

3. It spawned a fresh `ffmpeg -ss <n*12> -i <remote url>` for every 12-second
   segment. Each spawn re-opened the remote debrid URL and re-sought from
   scratch, which is why playback was slow and fell over under seeking. A
   `Session` here keeps one ffmpeg alive writing segments to a temp dir, and is
   restarted only when the viewer seeks outside what it has produced.

Note on copying video in HLS mode: it is deliberately not done. Segment
boundaries must land on keyframes, and with `-c:v copy` ffmpeg cannot place them
-- the playlist we hand the player would drift out of sync with the segments it
receives. Getting it right needs a keyframe index, and building one means
reading the whole file off the provider. Video-copy is therefore reserved for
the progressive path, where no segmentation is involved.
"""

from __future__ import annotations

import asyncio
import json
import math
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger
from pydantic import BaseModel

from program.services.streaming.playback_url import redact

# Segment length. Shorter means faster startup and cheaper seeks, at the cost of
# more requests; 6s is the common default and matches what hls.js expects.
SEGMENT_DURATION = 6

# How far ahead of the running session a requested segment may be before it is
# cheaper to restart ffmpeg at that point than to wait for it to catch up.
RESTART_THRESHOLD_SEGMENTS = 3

# How long to wait for a segment the running session should be about to write.
SEGMENT_WAIT_TIMEOUT = 60.0

# Idle sessions hold a temp dir and an ffmpeg process; reap them.
SESSION_IDLE_TIMEOUT = 120.0

# Codecs a mainstream browser can be expected to decode. Anything outside these
# has to be re-encoded. This is only the *candidate* set -- the client still
# confirms with canPlayType against the precise codec string.
BROWSER_VIDEO_CODECS = {"h264", "vp8", "vp9", "av1"}
BROWSER_AUDIO_CODECS = {"aac", "mp3", "opus", "vorbis", "flac"}

# Containers a browser will accept for progressive playback.
BROWSER_CONTAINERS = {"mp4", "mov", "m4v", "webm"}

# ffprobe's format_name for an MP4-family file is always the comma list
# "mov,mp4,m4a,3gp,3g2,mj2" -- QuickTime is the umbrella format MP4 was built
# on, and ffprobe lists it first regardless of the file's actual extension.
# Picking whichever entry comes first would report every such file as "mov".
# A browser doesn't care what string this is, but real Jellyfin clients match
# the reported Container against their own DirectPlayProfile container lists
# before ever requesting the stream -- and those lists say "mp4", never
# "mov". Reporting "mov" made every native client refuse the source as
# incompatible while the web player (which never looks at this field) played
# the same file fine. Highest priority first.
CONTAINER_PRIORITY = ("mp4", "webm", "m4v", "mov")

# ffprobe's format_name for a Matroska file is "matroska,webm" -- WebM is a
# constrained profile of Matroska, so ffprobe always lists it as a second,
# valid reading of the same bytes. CONTAINER_PRIORITY alone would pick "webm"
# here too, for the same reason it picked "mov": it is the one name in the
# list that BROWSER_CONTAINERS (and a naive priority order) recognises.
# But a real MKV is not a WebM -- reporting it as one to a client is the same
# mistake as reporting "mov" for an MP4, just for a container browsers were
# never going to direct-play anyway (mkv is not in BROWSER_CONTAINERS), so it
# only surfaced once ffprobe was actually asked, on "Pirates" -- a Bluray
# remux MKV. Checked ahead of CONTAINER_PRIORITY, and only for an ffprobe
# format_name that pairs "matroska" with "webm"; a real WebM file's
# format_name is just "webm" alone, with no "matroska" alongside it.
def _pick_container(names: list[str], present: set[str]) -> str | None:
    if "matroska" in present:
        return "mkv"

    return next(
        (n for n in CONTAINER_PRIORITY if n in present),
        names[0] if names else None,
    )


@dataclass(frozen=True)
class Capabilities:
    """What one client can play without help.

    A parameter rather than module constants because the playback decision
    needs BOTH halves -- what the file contains and what this particular
    client accepts -- and the second half is not a property of this server.
    A browser, an Apple TV and a Roku have genuinely different answers, and a
    Jellyfin client states its own in the DeviceProfile it sends us.

    Hardcoding one client's answer is the bug this codebase already hit once,
    from the other direction: the old player asked the browser about HEVC and
    never looked at the file, so Firefox transcoded everything.
    """

    video_codecs: frozenset[str]
    audio_codecs: frozenset[str]
    containers: frozenset[str]

    def plays_video(self, codec: str | None) -> bool:
        return (codec or "") in self.video_codecs

    def plays_audio(self, codec: str | None) -> bool:
        # A file with no audio track at all is fine to play as-is.
        return codec is None or codec in self.audio_codecs

    def plays_container(self, container: str | None) -> bool:
        return bool(container and container in self.containers)


#: The default, and what every existing caller gets. Same three sets as before.
BROWSER = Capabilities(
    video_codecs=frozenset(BROWSER_VIDEO_CODECS),
    audio_codecs=frozenset(BROWSER_AUDIO_CODECS),
    containers=frozenset(BROWSER_CONTAINERS),
)


class MediaProbe(BaseModel):
    """What ffprobe could tell us about the file."""

    duration: float = 0.0
    video_codec: str | None = None
    audio_codec: str | None = None
    container: str | None = None
    width: int | None = None
    height: int | None = None

    @property
    def video_playable(self) -> bool:
        return (self.video_codec or "") in BROWSER_VIDEO_CODECS

    @property
    def audio_playable(self) -> bool:
        # A file with no audio track at all is fine to play as-is.
        return self.audio_codec is None or self.audio_codec in BROWSER_AUDIO_CODECS

    @property
    def container_playable(self) -> bool:
        return bool(self.container and self.container in BROWSER_CONTAINERS)


class PlaybackInfo(BaseModel):
    """
    What the client needs in order to choose a playback mode itself.

    `mode` is the server's recommendation; the client is expected to confirm
    `direct` against its own `canPlayType`, because codec support genuinely
    differs between browsers and only the browser knows for sure.
    """

    item_id: int
    probe: MediaProbe
    mode: str  # "direct" | "remux" | "transcode"
    # An RFC 6381 codec string for canPlayType, when we can build one.
    mime_type: str | None = None
    reason: str


def _ffprobe(url: str) -> MediaProbe:
    """Read stream metadata without downloading the file."""

    cmd = [
        "ffprobe",
        "-v",
        "error",
        # Enough to see the moov atom and the first frames, not the whole file.
        "-analyzeduration",
        "5000000",
        "-probesize",
        "10000000",
        "-show_entries",
        "format=duration,format_name:stream=codec_type,codec_name,width,height",
        "-of",
        "json",
        url,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        logger.warning(f"ffprobe timed out for {redact(url)}")
        return MediaProbe()

    if result.returncode != 0:
        # ffprobe echoes the URL it was given back into stderr, so the
        # message needs redacting too -- redacting only the `url` argument
        # leaves the provider token in the log via the error text. Same trap
        # as the debrid link leak already fixed in the streaming router.
        logger.warning(
            f"ffprobe failed for {redact(url)}: "
            f"{redact(result.stderr.strip())[:200]}"
        )
        return MediaProbe()

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return MediaProbe()

    probe = MediaProbe()
    fmt = data.get("format") or {}

    try:
        probe.duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        probe.duration = 0.0

    # format_name is a comma-separated list, e.g. "mov,mp4,m4a,3gp,3g2,mj2".
    names = (fmt.get("format_name") or "").split(",")
    present = set(names)
    probe.container = _pick_container(names, present)

    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "video" and not probe.video_codec:
            probe.video_codec = stream.get("codec_name")
            probe.width = stream.get("width")
            probe.height = stream.get("height")
        elif stream.get("codec_type") == "audio" and not probe.audio_codec:
            probe.audio_codec = stream.get("codec_name")

    return probe


# Probing costs a remote round trip, so results are memoised per file. The
# codecs inside a file never change; only the URL in front of it does.
_probe_cache: dict[str, MediaProbe] = {}


def probe(url: str, cache_key: str) -> MediaProbe:
    """Probe `url`, reusing an earlier result for the same `cache_key`."""

    cached = _probe_cache.get(cache_key)

    if cached is not None:
        return cached

    result = _ffprobe(url)

    # Don't memoise a failed probe -- the URL may simply have been stale, and a
    # later attempt with a fresh one should get another chance.
    if result.duration or result.video_codec:
        _probe_cache[cache_key] = result

    return result


def _mime_for(probe_result: MediaProbe) -> str | None:
    """Build an RFC 6381 type string so the client can call canPlayType."""

    if not probe_result.video_codec:
        return None

    # Generic profile strings: enough for canPlayType to answer usefully
    # without parsing SPS/PPS out of the bitstream.
    video = {
        "h264": "avc1.640029",
        "hevc": "hvc1.1.6.L93.B0",
        "h265": "hvc1.1.6.L93.B0",
        "vp9": "vp09.00.10.08",
        "av1": "av01.0.05M.08",
        "vp8": "vp8",
    }.get(probe_result.video_codec)

    if not video:
        return None

    audio = {"aac": "mp4a.40.2", "mp3": "mp4a.40.34", "opus": "opus", "flac": "flac"}.get(
        probe_result.audio_codec or ""
    )

    container = "video/webm" if probe_result.container == "webm" else "video/mp4"
    codecs = ", ".join(c for c in (video, audio) if c)

    return f'{container}; codecs="{codecs}"'


def decide(
    probe_result: MediaProbe, caps: Capabilities = BROWSER
) -> tuple[str, str]:
    """
    Choose a playback mode from what the file contains and what `caps` accepts.

    Returns (mode, human-readable reason). `caps` defaults to a mainstream
    browser, which is what every caller wanted before clients other than the
    web player existed.
    """

    if not probe_result.video_codec:
        # Probe failed. Direct play is the cheap guess and degrades to a normal
        # media error rather than burning CPU on a transcode that may also fail.
        return "direct", "could not probe the file; attempting direct play"

    if not caps.plays_video(probe_result.video_codec):
        return (
            "transcode",
            f"video codec {probe_result.video_codec} is not supported by this client",
        )

    if not caps.plays_audio(probe_result.audio_codec):
        return (
            "remux",
            f"video is {probe_result.video_codec} but audio codec "
            f"{probe_result.audio_codec} needs converting",
        )

    if not caps.plays_container(probe_result.container):
        return (
            "remux",
            f"streams are playable but the {probe_result.container} container is not",
        )

    return "direct", f"{probe_result.video_codec}/{probe_result.audio_codec} plays natively"


@dataclass
class Session:
    """
    One long-lived ffmpeg process producing HLS segments for one item.

    Segments are written to a temp dir as `seg{n}.ts`, numbered so that `n`
    matches the segment index in the playlist we serve. Because video is
    re-encoded with forced keyframes at exactly `SEGMENT_DURATION`, segment `n`
    always covers `[n*SEGMENT_DURATION, (n+1)*SEGMENT_DURATION)` and the
    precomputed VOD playlist stays truthful.
    """

    item_id: int
    url: str
    start_seq: int
    copy_video: bool
    copy_audio: bool
    directory: Path
    process: asyncio.subprocess.Process | None = None
    last_access: float = field(default_factory=time.monotonic)

    def segment_path(self, seq: int) -> Path:
        return self.directory / f"seg{seq}.ts"

    def covers(self, seq: int) -> bool:
        """Is `seq` inside the range this session is producing?"""

        return seq >= self.start_seq

    def is_running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    def build_command(self) -> list[str]:
        start_time = self.start_seq * SEGMENT_DURATION

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            # Reconnect: debrid CDNs drop long-lived connections routinely, and
            # losing one mid-session used to kill the whole playback.
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
            "-analyzeduration",
            "5000000",
            "-probesize",
            "10000000",
        ]

        # Seeking before -i is the fast path: ffmpeg uses byte-range requests to
        # jump rather than decoding everything up to the target.
        if start_time:
            cmd += ["-ss", str(start_time)]

        cmd += ["-i", self.url]

        if self.copy_video:
            cmd += ["-c:v", "copy"]
        else:
            cmd += [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-profile:v",
                "high",
                "-level",
                "4.1",
                # Exact segment boundaries. Without this the encoder places
                # keyframes on its own schedule and segments drift from the
                # durations advertised in the playlist.
                "-force_key_frames",
                f"expr:gte(t,n_forced*{SEGMENT_DURATION})",
            ]

        cmd += ["-c:a", "copy"] if self.copy_audio else ["-c:a", "aac", "-b:a", "160k", "-ac", "2"]

        cmd += [
            "-f",
            "hls",
            "-hls_time",
            str(SEGMENT_DURATION),
            "-hls_playlist_type",
            "vod",
            "-hls_list_size",
            "0",
            "-hls_flags",
            "independent_segments",
            # Number segments from the seek point so filenames line up with the
            # playlist indices the client is asking for.
            "-start_number",
            str(self.start_seq),
            "-hls_segment_filename",
            str(self.directory / "seg%d.ts"),
            str(self.directory / "stream.m3u8"),
        ]

        return cmd

    async def start(self) -> None:
        cmd = self.build_command()

        logger.debug(
            f"HLS session for item {self.item_id} from segment {self.start_seq} "
            f"(video={'copy' if self.copy_video else 'x264'}, "
            f"audio={'copy' if self.copy_audio else 'aac'})"
        )

        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

    async def stop(self) -> None:
        if self.process and self.process.returncode is None:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.kill()
            except ProcessLookupError:
                pass

        shutil.rmtree(self.directory, ignore_errors=True)

    async def read_error(self) -> str:
        if not self.process or not self.process.stderr:
            return ""

        try:
            data = await asyncio.wait_for(self.process.stderr.read(4096), timeout=1)
            return data.decode(errors="replace").strip()
        except (asyncio.TimeoutError, Exception):
            return ""

    async def wait_for_segment(self, seq: int) -> bytes | None:
        """
        Wait until segment `seq` is fully written, then return it.

        ffmpeg writes a segment incrementally, so a file existing is not proof
        it is complete. The next segment appearing is: ffmpeg only opens
        `seg{n+1}` once `seg{n}` is closed.
        """

        path = self.segment_path(seq)
        next_path = self.segment_path(seq + 1)
        playlist = self.directory / "stream.m3u8"
        deadline = time.monotonic() + SEGMENT_WAIT_TIMEOUT

        while time.monotonic() < deadline:
            if path.exists() and (next_path.exists() or self._finished(playlist)):
                return path.read_bytes()

            if not self.is_running():
                # Process exited. Either it finished the file (segment is
                # complete and just was not followed by another) or it died.
                if path.exists():
                    return path.read_bytes()

                error = await self.read_error()
                logger.error(
                    f"HLS session for item {self.item_id} exited before segment {seq}"
                    + (f": {error}" if error else "")
                )
                return None

            await asyncio.sleep(0.25)

        logger.error(f"Timed out waiting for segment {seq} of item {self.item_id}")
        return None

    @staticmethod
    def _finished(playlist: Path) -> bool:
        """Has ffmpeg written the end-of-playlist marker?"""

        try:
            return "#EXT-X-ENDLIST" in playlist.read_text()
        except OSError:
            return False


class SessionManager:
    """Owns the live HLS sessions, one per item."""

    def __init__(self) -> None:
        self._sessions: dict[int, Session] = {}
        self._lock = asyncio.Lock()

    async def segment(
        self,
        *,
        item_id: int,
        seq: int,
        url: str,
        copy_video: bool,
        copy_audio: bool,
    ) -> bytes | None:
        """
        Return segment `seq`, starting or restarting a session if needed.

        A session is restarted when the viewer seeks backwards, or forwards past
        what the running session will reach soon.
        """

        async with self._lock:
            await self._reap_idle()

            session = self._sessions.get(item_id)
            produced = self._highest_produced(session) if session else -1

            needs_restart = (
                session is None
                or not session.covers(seq)
                or session.url != url
                or (not session.is_running() and not session.segment_path(seq).exists())
                or seq > produced + RESTART_THRESHOLD_SEGMENTS
            )

            if needs_restart:
                if session:
                    await session.stop()

                session = Session(
                    item_id=item_id,
                    url=url,
                    start_seq=seq,
                    copy_video=copy_video,
                    copy_audio=copy_audio,
                    directory=Path(tempfile.mkdtemp(prefix=f"riven-hls-{item_id}-")),
                )

                await session.start()
                self._sessions[item_id] = session

            session.last_access = time.monotonic()

        return await session.wait_for_segment(seq)

    @staticmethod
    def _highest_produced(session: Session) -> int:
        """Index of the newest segment the session has opened so far."""

        highest = session.start_seq - 1

        try:
            for path in session.directory.glob("seg*.ts"):
                try:
                    highest = max(highest, int(path.stem[3:]))
                except ValueError:
                    continue
        except OSError:
            pass

        return highest

    async def _reap_idle(self) -> None:
        now = time.monotonic()

        for item_id, session in list(self._sessions.items()):
            if now - session.last_access > SESSION_IDLE_TIMEOUT:
                await session.stop()
                self._sessions.pop(item_id, None)

    async def stop(self, item_id: int) -> None:
        async with self._lock:
            if session := self._sessions.pop(item_id, None):
                await session.stop()

    async def stop_all(self) -> None:
        async with self._lock:
            for session in self._sessions.values():
                await session.stop()

            self._sessions.clear()


def build_playlist(duration: float) -> str:
    """A static VOD playlist covering `duration` in `SEGMENT_DURATION` chunks."""

    if duration <= 0:
        # Unknown duration: advertise a short playlist so the player at least
        # starts. Seeking will be limited, which beats not playing at all.
        count = 10
        remainder = 0.0
    else:
        count = math.ceil(duration / SEGMENT_DURATION)
        remainder = duration - (count - 1) * SEGMENT_DURATION

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{SEGMENT_DURATION}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-INDEPENDENT-SEGMENTS",
    ]

    for i in range(count):
        length = remainder if (i == count - 1 and remainder > 0) else float(SEGMENT_DURATION)
        lines.append(f"#EXTINF:{length:.6f},")
        lines.append(f"segment/{i}.ts")

    lines.append("#EXT-X-ENDLIST")

    return "\n".join(lines)


def build_remux_command(url: str, start_time: float = 0.0) -> list[str]:
    """
    Progressive fragmented-MP4 remux: keep the video, fix the wrapper.

    Used when the video stream is already something the browser decodes and only
    the audio codec or the container is in the way. Copying the video costs
    almost nothing next to an x264 re-encode of the same file.
    """

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "5",
    ]

    if start_time > 0:
        cmd += ["-ss", str(start_time)]

    cmd += [
        "-i",
        url,
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-ac",
        "2",
        # Fragmented MP4 so it can be streamed without a seekable output.
        # The flag is `default_base_moof`, not `default_base_is_moof` -- ffmpeg
        # rejects the latter as an undefined constant and writes nothing.
        "-movflags",
        "frag_keyframe+empty_moov+default_base_moof",
        "-f",
        "mp4",
        "-",
    ]

    return cmd
