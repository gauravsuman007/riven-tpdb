"""Turn a client's DeviceProfile into the capability set `decide()` wants.

This is the whole reason the masquerade can make good playback decisions. A
browser has to be guessed at; a Jellyfin client TELLS us, in the DeviceProfile
it posts to /Items/{id}/PlaybackInfo, exactly which container/codec
combinations it plays untouched.

The direction of the data matters. `DirectPlayProfiles` is a list of
"container X with video codec Y and audio codec Z is fine", and we flatten it
into three sets. Flattening loses the correlation -- a client that plays h264
only in mp4 and vp9 only in webm is recorded as playing both codecs in both
containers -- which can mean we offer a direct stream the client then refuses.
That is the right way to be wrong: the client falls back and asks for a
transcode, costing one round trip. Preserving the correlation exactly would
mean reimplementing Jellyfin's stream-selection matrix, which is a large amount
of logic for that one round trip.
"""

from typing import Any

from program.services.streaming.transcode import BROWSER, Capabilities


def _split(value: Any) -> list[str]:
    """DeviceProfile fields are comma-separated strings, sometimes lists."""

    if not value:
        return []

    if isinstance(value, list):
        parts = value
    else:
        parts = str(value).split(",")

    return [p.strip().lower() for p in parts if p and str(p).strip()]


def from_device_profile(profile: dict[str, Any] | None) -> Capabilities:
    """Fold DirectPlayProfiles into video/audio/container sets.

    Falls back to browser capabilities when the client sends no profile at
    all. That is a real case -- some clients omit it on the first call -- and
    the browser set is the conservative choice: it is small, so the failure
    mode is an unnecessary transcode rather than a stream the client cannot
    decode and shows as a black screen.
    """

    if not profile:
        return BROWSER

    direct = profile.get("DirectPlayProfiles") or []

    if not direct:
        return BROWSER

    containers: set[str] = set()
    video: set[str] = set()
    audio: set[str] = set()

    for entry in direct:
        if not isinstance(entry, dict):
            continue

        # "Photo"/"Audio" profiles appear in the same list and say nothing
        # about what video this client can render.
        if (entry.get("Type") or "Video").strip().lower() != "video":
            continue

        containers.update(_split(entry.get("Container")))
        video.update(_split(entry.get("VideoCodec")))
        audio.update(_split(entry.get("AudioCodec")))

    if not video or not containers:
        return BROWSER

    return Capabilities(
        video_codecs=frozenset(video),
        audio_codecs=frozenset(audio),
        containers=frozenset(containers),
    )
