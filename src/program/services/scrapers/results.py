"""The shape a scraper hands back for one release.

Scrapers used to return ``dict[str, str]`` -- infohash to raw title -- which
threw away everything an indexer says about a release beyond its name. Seeders
in particular are what separates "this download is slow" from "this download
will never happen", and that distinction was unavailable to the downloader and
invisible in the UI.

Every field except ``raw_title`` is optional and stays ``None`` when the source
does not report it. That matters most for ``seeders``: a missing count means
"unknown", not "nobody is seeding", and treating the two the same would abandon
healthy releases from any indexer that omits the field.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ScrapeResult:
    """One release as an indexer described it."""

    raw_title: str
    seeders: int | None = None
    leechers: int | None = None
    size: int | None = None
    """Size in bytes, as reported. Indexers lie about this often enough that it
    is shown to the user rather than used for filtering."""
    indexer: str | None = None
    """Human-readable indexer name, so a user can tell where a release came
    from -- and which indexer to stop trusting."""
    download_url: str | None = None
    """Where the actual .torrent file can be fetched, when the indexer offers one.

    Worth carrying all the way to the debrid service. Riven hands the provider
    a bare `magnet:?xt=urn:btih:<hash>` with NO trackers, which leaves it
    nothing but the DHT to find peers with -- and a swarm that announces only
    to its own tracker has no DHT presence at all. The torrent file has the
    announce list, and the difference is not marginal: the same release sat at
    "stalled (no seeds), 0 peers" as a magnet and downloaded at 4.3 MB/s from
    three seeders once the file itself was uploaded.
    """
    privacy: str | None = None
    """"public", "semiPrivate" or "private", as the indexer declares itself.

    This is not trivia. A debrid service is handed the infohash and joins the
    swarm from its own network with no account anywhere, so a release on a
    private or semi-private tracker is unreachable to it no matter how many
    seeders the indexer reports -- they are all announcing to a passkey-gated
    tracker it cannot use. Measured on a real pick: PornoLab reported 19
    seeders, TorBox found 0 and stayed at "stalled (no seeds)" indefinitely.
    """
