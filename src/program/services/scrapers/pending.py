"""Turning a placeholder candidate into a real infohash, only when chosen.

Some trackers (PornoLab) cap how many .torrent files an account may download
per day, and the file is the only place their search results carry a hash. So a
search lists such a release under a placeholder id (`download_quota.pending_key`)
and spends nothing. A download is spent in exactly two situations:

* a person picks the release (`queue_release`);
* the automatic picker's best choice is one of them (`settle_unresolved`).

Everything else is discarded without ever being fetched.
"""

import hashlib

import bencodepy
import requests
from loguru import logger

from program.media.stream import Stream
from program.services.scrapers.download_quota import ledger
from program.services.scrapers.quota import QuotaStop, tracker


class PendingResolveError(Exception):
    """The real infohash could not be had; the message is fit to show a user."""


def resolve_pending(key: str, *, manual: bool) -> str:
    """Download the .torrent behind a placeholder id and return its infohash.

    ``manual`` may use the slot the automatic pipeline leaves in reserve; the
    automatic picker may not, so a person can always still fetch one by hand.
    The file is kept on disk so uploading it to the debrid service later does
    not cost a second download.
    """

    details = ledger.pending(key)

    if details is None:
        raise PendingResolveError(
            "That release is no longer in the candidate list. Run the scrape "
            "again and pick it from the fresh results."
        )

    guid, url, indexer = details["guid"], details["download_url"], details["indexer"]

    if known := ledger.infohash(guid):
        return known

    # Already downloaded once (and kept): nothing to spend.
    if (kept := ledger.torrent_file(url)) is not None:
        try:
            return hashlib.sha1(
                bencodepy.encode(bencodepy.decode(kept)[b"info"])
            ).hexdigest()
        except Exception:
            pass

    # Gate on the tracker's live allowance. Anything that goes wrong after this
    # point pauses the indexer (`end_grab(ok=False)`) rather than being retried.
    try:
        reservation = tracker.begin_grab(indexer, manual=manual)
    except QuotaStop as e:
        raise PendingResolveError(str(e)) from e

    try:
        response = requests.get(url, timeout=30, allow_redirects=True)
        content = response.content if response.ok else b""
    except Exception as e:
        tracker.end_grab(indexer, reservation, ok=False)
        raise PendingResolveError(f"Could not reach {indexer}: {e}") from e

    try:
        if not content.startswith(b"d"):
            raise ValueError("not a torrent file")

        infohash = hashlib.sha1(
            bencodepy.encode(bencodepy.decode(content)[b"info"])
        ).hexdigest()
    except Exception:
        # A page instead of a file: the tracker refused, whatever its count
        # said. Not worth parsing why -- pause, and re-read the allowance.
        tracker.end_grab(indexer, reservation, ok=False)
        raise PendingResolveError(
            f"{indexer} refused the download, so downloads from it are paused "
            "for a while. Try again later."
        ) from None

    tracker.end_grab(indexer, reservation, ok=True)
    ledger.remember(guid, infohash)
    ledger.keep_torrent_file(url, content)

    return infohash


def settle_unresolved(streams: dict[str, Stream]) -> dict[str, Stream]:
    """The automatic picker's view of a scrape: no placeholders left in it.

    ``streams`` is in pick order. Placeholders ranked ABOVE the best release
    that already has a hash are the ones the picker would have chosen, so each
    of those is fetched (until the budget says stop) and takes its place;
    everything else that is still a placeholder is dropped unfetched.
    """

    if not any(s.unresolved for s in streams.values()):
        return streams

    settled: dict[str, Stream] = {}
    seen_resolved = False

    for key, stream in streams.items():
        if not stream.unresolved:
            seen_resolved = True
            settled[key] = stream
            continue

        if seen_resolved:
            continue

        try:
            infohash = resolve_pending(key, manual=False)
        except PendingResolveError as e:
            logger.debug(f"Not fetching {stream.raw_title}: {e}")
            continue

        stream.infohash = infohash
        stream.unresolved = False
        settled[infohash] = stream
        seen_resolved = True  # one fetch is the pick; nothing below it is needed

    return settled
