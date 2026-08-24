"""What a direct-site scraper hands back.

These sites are not indexers: there is no infohash, no swarm and no debrid
step. A result is a page on a streaming site, and playing it means resolving
that page down to an actual media URL.

The two halves are deliberately separate. Search results are cheap, cacheable
and safe to show in bulk; a resolved source is expensive to obtain, usually
short-lived, and on some sites is bound to the IP that requested it. Resolving
at search time would mean every link in the grid had expired by the time the
user clicked one.
"""

from dataclasses import dataclass, field, replace


@dataclass(frozen=True, slots=True)
class DirectVideo:
    """One video as a site's search results described it."""

    site: str
    """Scraper key, e.g. ``xfreehd``. Also what /resolve is keyed on."""
    video_id: str
    title: str
    page_url: str
    thumbnail: str | None = None
    duration: int | None = None
    """Runtime in seconds. ``None`` when the site did not say."""
    resolution: str | None = None
    """Normalised, e.g. ``1080p``. ``None`` rather than a guess -- most sites
    only expose the real figure on the video page, and claiming "HD" means
    anything from 720p to 4K."""
    size: int | None = None
    """Size in bytes of the best source, when the site reports it up front."""
    views: int | None = None
    hd: bool = False
    """The site showed an HD badge. Kept separate from ``resolution`` because
    it is a claim rather than a measurement -- the same badge covers 720p and
    4K -- but it still orders a result above one with no quality signal."""
    relevance: float | None = None
    """How well this matched the query, filled in by the ranker. ``None`` on a
    result that has not been scored yet."""

    def key(self) -> str:
        return f"{self.site}:{self.video_id}"

    def with_relevance(self, score: float) -> "DirectVideo":
        return replace(self, relevance=score)


@dataclass(frozen=True, slots=True)
class DirectSource:
    """One playable rendition of a video."""

    url: str
    label: str
    """What to show in a quality picker, e.g. ``1080p`` or ``HD``."""
    resolution: str | None = None
    size: int | None = None
    mime_type: str = "video/mp4"
    """What the URL actually serves. Not always an MP4: one site hands back an
    HLS playlist for some videos, and a player told to expect MP4 shows a blank
    frame rather than an error."""
    headers: dict[str, str] = field(default_factory=dict)
    """Headers the upstream requires -- Referer, mostly. Several of these CDNs
    return 403 without one, so the value travels with the URL rather than being
    reconstructed by whoever fetches it."""
