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
