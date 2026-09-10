"""Adult Empire as a metadata provider: a local title index, then one page.

Why an index at all
-------------------
Adult Empire has a search. We may not use it: their robots.txt disallows
every ``/Search`` path, and this crawler exists on the terms their robots.txt
sets (see the module docstring in `adultempire.py`, which makes the same
point about the age/terms interstitial). What robots.txt *does* publish is a
sitemap, and every product URL carries the title in its slug:

    /995/pam-tommy-lee-hardcore-porn-movies.html

So the whole catalogue can be indexed from sitemaps alone -- no product page
is read to build it -- and a lookup then costs at most a handful of detail
fetches for titles that already matched on slug. That is permitted, and it is
far less traffic than searching would have been.

The cost is the build: ~1300 sitemap pages at one request a second, so about
twenty minutes, once a month. It is cached on disk and rebuilt on age.

Why the slug is only used for matching
--------------------------------------
A slug has lost its case, punctuation and any distinction between "&" and
"and". It is good enough to find candidates and useless as metadata, so the
real title, studio, year and cast always come from the detail page.
"""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

from loguru import logger

from program.services.recommendations.adultempire import (
    AdultEmpireClient,
    AdultEmpireError,
    RankedTitle,
    parse_detail,
)

SITEMAP_INDEX = "https://www.adultdvdempire.com/sitemaps/movies/sitemap_index.xml"

_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

# Every movie URL is /<id>/<slug>.html, and the slug always carries this
# suffix. Stripping it is what turns the slug back into something comparable
# to a real title.
_PRODUCT_URL = re.compile(r"/(\d+)/([a-z0-9-]+?)(?:-porn-movies)?\.html$")

# How many slug-matched candidates are worth a detail fetch. Each one is a
# request against someone's shop, so this stays small: the slug ranking is
# good enough that the right title is first or not present at all.
DETAIL_CANDIDATES = 3

# Below this, the slug is not the same title and fetching its page would be
# pure noise.
MIN_SLUG_RATIO = 0.75


@dataclass(slots=True)
class IndexEntry:
    product_id: str
    url: str
    slug_text: str


def normalise(text: str) -> str:
    """Reduce a title to what a slug preserves: lowercase words, nothing else."""

    text = text.replace("&", " and ")

    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def _entry_from_url(url: str) -> IndexEntry | None:
    match = _PRODUCT_URL.search(url)

    if not match:
        return None

    return IndexEntry(
        product_id=match.group(1),
        url=url,
        slug_text=match.group(2).replace("-", " "),
    )


def build_index(
    client: AdultEmpireClient,
    *,
    sitemap_limit: int = 0,
) -> list[IndexEntry]:
    """Read the movie sitemaps and return every product they list.

    `sitemap_limit` of 0 means all of them. Any value above 0 truncates the
    crawl, which is there for a usable first run rather than as a normal mode:
    a truncated index silently cannot resolve whatever it did not reach.
    """

    body = client._get(SITEMAP_INDEX)
    pages = [
        loc.text.strip()
        for loc in ET.fromstring(body).findall(".//sm:sitemap/sm:loc", _SITEMAP_NS)
        if loc.text
    ]

    if sitemap_limit:
        pages = pages[:sitemap_limit]

    entries = dict[str, IndexEntry]()
    started = time.monotonic()

    for number, page in enumerate(pages, start=1):
        try:
            page_body = client._get(page)
        except AdultEmpireError as exc:
            # One bad sitemap page costs its 100 titles, not the whole index.
            logger.debug(f"Adult Empire sitemap {page} failed: {exc}")
            continue

        try:
            locations = ET.fromstring(page_body).findall(".//sm:url/sm:loc", _SITEMAP_NS)
        except ET.ParseError as exc:
            logger.debug(f"Adult Empire sitemap {page} is not valid XML: {exc}")
            continue

        for loc in locations:
            if not loc.text:
                continue

            entry = _entry_from_url(loc.text.strip())

            if entry:
                entries[entry.product_id] = entry

        if number % 100 == 0:
            logger.debug(
                f"Adult Empire index: {number}/{len(pages)} sitemaps, "
                f"{len(entries)} titles, {time.monotonic() - started:.0f}s"
            )

    return list(entries.values())


def save_index(entries: list[IndexEntry], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "entries": [[e.product_id, e.url, e.slug_text] for e in entries],
    }

    # Written beside the target and moved into place: a twenty-minute build
    # interrupted halfway must not leave a half-written index that parses.
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload))
    temporary.replace(path)


def load_index(path: Path, max_age_days: int) -> list[IndexEntry] | None:
    """The cached index, or None when it is missing, unreadable or stale."""

    if not path.exists():
        return None

    try:
        payload = json.loads(path.read_text())
        built_at = datetime.fromisoformat(payload["built_at"])
    except Exception as exc:
        logger.debug(f"Adult Empire index at {path} is unreadable: {exc}")
        return None

    if datetime.now(timezone.utc) - built_at > timedelta(days=max_age_days):
        return None

    return [IndexEntry(*row) for row in payload.get("entries", [])]


def rank_candidates(
    entries: list[IndexEntry],
    title: str,
    *,
    limit: int = DETAIL_CANDIDATES,
) -> list[IndexEntry]:
    """The index entries whose slug looks most like `title`."""

    wanted = normalise(title)

    if not wanted:
        return []

    scored = list[tuple[float, IndexEntry]]()

    for entry in entries:
        slug = entry.slug_text

        # A cheap gate before the expensive comparison: the whole catalogue is
        # ~130k rows, and SequenceMatcher on every one of them per lookup is
        # not affordable.
        if wanted not in slug and slug not in wanted:
            first = wanted.split(" ", 1)[0]

            if first not in slug:
                continue

        ratio = SequenceMatcher(None, wanted, slug).ratio()

        if ratio >= MIN_SLUG_RATIO:
            scored.append((ratio, entry))

    scored.sort(key=lambda pair: pair[0], reverse=True)

    return [entry for _, entry in scored[:limit]]


def fetch_detail(client: AdultEmpireClient, entry: IndexEntry) -> RankedTitle | None:
    """Read one product page into a RankedTitle, or None if it cannot be read."""

    item = RankedTitle(
        product_id=entry.product_id,
        # Deliberately blank: `parse_detail` fills the real title from the
        # page only when the caller has none, and the slug is not a title.
        title="",
        rank=0,
        listing="lookup",
        url=entry.url,
    )

    try:
        body = client._get(entry.url)
    except AdultEmpireError as exc:
        logger.debug(f"Adult Empire detail {entry.url} failed: {exc}")
        return None

    return parse_detail(body, item)


def build_and_save_index() -> int:
    """Rebuild the cached title index from the sitemaps. Returns its size.

    The long job: ~1300 sitemap reads at a one-second courtesy delay, so
    roughly twenty minutes. Nothing waits on it -- lookups skip the provider
    while the index is missing rather than triggering this inline.
    """

    from program.settings import settings_manager

    settings = settings_manager.settings.adultempire_metadata
    client = AdultEmpireClient()

    entries = build_index(client, sitemap_limit=settings.index_sitemap_limit)

    if not entries:
        # Writing an empty index would look fresh and answer nothing until it
        # aged out, which is worse than having none at all.
        logger.warning("Adult Empire index build produced nothing; keeping the old one.")
        return 0

    save_index(entries, settings.index_path)
    logger.info(f"Adult Empire index rebuilt: {len(entries)} titles.")

    return len(entries)
