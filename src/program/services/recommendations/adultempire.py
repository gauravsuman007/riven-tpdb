"""Adult Empire: ranked catalogue data, which TPDB has none of.

TPDB exposes no ranking whatsoever -- ``rating`` is 0 on every record and
``order_by`` is silently ignored -- so every "best of" surface in this fork has
to be derived from somewhere else. Adult Empire is a storefront, and a
storefront knows things a metadata database does not: what sells, what is
trending, what customers rated, and what they bought together.

Four surfaces are useful here:

    * ``all-time-bestselling`` -- a durable quality proxy. Rank 1 is *Pirates*,
      which is the right answer, so the ordering is real rather than churn.
    * ``best-selling`` / ``trending`` -- what is moving right now.
    * per-title ``rating-stars-avg`` -- an actual audience score.
    * "Customers Who Bought This Product Also Bought" -- collaborative
      filtering, which is a genuinely different signal from TPDB's ``/similar``
      (that one is metadata similarity; this one is behaviour).

Access rules, which are not incidental:

    * The site serves an age/terms interstitial to browser user agents. We do
      NOT click through it -- that button accepts the site's Terms &
      Conditions, which is not ours to accept. Instead we identify honestly as
      a bot, which the site's own robots.txt invites (``User-agent: *``), and
      it serves the real page.
    * robots.txt disallows every ``/Search`` path. Nothing here searches; the
      sitemap and the browse listings provide the same reach and are allowed.
    * Requests are serialised with a delay. This is someone's shop, not an API.
"""

import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from loguru import logger

BASE_URL = "https://www.adultdvdempire.com"

# Honest identification. Deliberately not a browser string: pretending to be a
# browser is what gets the terms interstitial, and pretending to be Googlebot
# would be impersonation. This says what we are, and the site serves it.
USER_AGENT = "Riven-TPDB-Crawler/1.0"

# Their shop, their bandwidth. One request a second, single threaded.
REQUEST_DELAY_SECONDS = 1.0

# Ranked listings. Each page holds 48 items and the rank is the position.
LISTINGS = {
    "all-time-bestsellers": "/all-time-bestselling-porn-movies.html",
    "bestsellers": "/best-selling-porn-movies.html",
    "trending": "/trending-porn-movies.html",
    "new-releases": "/new-release-porn-movies.html",
}

ITEMS_PER_PAGE = 48

# Cards are located first and their fields extracted from the slice, rather
# than matched in one expression: an optional poster group inside a lazy match
# is simply skipped by the engine, which silently yielded no posters at all.
_CARD_START = re.compile(r'<div class="product-card" id="card(\d+)">')
_CARD_TITLE = re.compile(
    r'<div class="product-details__item-title"><a href="([^"]+)"[^>]*\s*title="([^"]*)"',
    re.S,
)
_CARD_POSTER = re.compile(r'src="(https://[^"]+?/products/[^"]+?\.jpg)"')

# Boxcovers come in sized variants; "m" is the grid thumbnail and "h" the large
# one. Asking for the large art up front avoids a second fetch per title.
_POSTER_SIZE = re.compile(r"(\d+)m\.jpg$")
_DETAIL_TITLE = re.compile(
    r'<h1 class="movie-page__heading__title">\s*(.*?)\s*</h1>', re.S
)
_RATING = re.compile(r'rating-stars-avg">\s*([\d.]+)\s*<')
_STUDIO = re.compile(r'/\d+/studio/[a-z0-9-]+\.html"[^>]*>\s*([^<]+?)\s*</a>')
_YEAR = re.compile(r"<small>Production Year:</small>\s*(\d{4})")
_RELEASED = re.compile(r"<small>Released:</small>\s*([A-Z][a-z]{2} \d{1,2} \d{4})")
_LENGTH = re.compile(r"<small>Length:\s*</small>\s*(?:(\d+)\s*hrs?\.)?\s*(?:(\d+)\s*mins?\.)?")
_CAST = re.compile(r'href="/\d+/[a-z0-9-]+-pornstars\.html"[^>]*>\s*([^<]+?)\s*</a>')
_ALSO_BOUGHT = re.compile(r'href="/(\d+)/([a-z0-9-]+)-porn-movies\.html"')
_MOVIE_HREF = re.compile(r"^/(\d+)/([a-z0-9-]+)-porn-movies\.html$")

# Studios. `/all-porn-movie-studios.html` on its own renders only about ten
# studios -- the page's default view is a curated top slice, not the
# catalogue. Adding `?letter=all` is what turns it into the actual directory:
# confirmed live, it returns every studio the catalogue has (800+ for movies,
# same again for videos, versus the ~100 the studio sitemaps cap out at,
# which is why a studio like Pure Taboo -- a real, working studio page --
# never showed up through the sitemaps at all). Confirmed non-paginated too:
# `?letter=all&page=2` returns the identical set.
#
# The three catalogues overlap heavily and are unioned by id. robots.txt
# disallows `/Search` and `/AllSearch/Search` specifically; these listing
# pages are not under either path.
STUDIO_INDEX_PAGES = (
    "/all-porn-movie-studios.html",
    "/all-porn-video-studios.html",
    "/all-blu-ray-studios.html",
)

# Sorts a studio page accepts. Deliberately not the full set the page offers
# (price, title, year, added) -- these are the two that rank by demand, which
# is the only thing worth a row of its own. There is no rating sort; the site
# carries a rating per title but will not order by it.
STUDIO_SORTS = ("bestseller", "trending")

_STUDIO_HREF = re.compile(r'href="(/(\d+)/studio/([a-z0-9-]+)\.html)"')
_STUDIO_NAME = re.compile(r'<h1 class="list-page__headline[^"]*">\s*(.*?)\s*</h1>', re.S)
_STUDIO_COUNT = re.compile(r'class="list-page__results"><strong>([\d,]+)</strong>')


@dataclass(slots=True)
class RankedTitle:
    """One entry from a ranked listing, before enrichment."""

    product_id: str
    title: str
    rank: int
    listing: str
    url: str

    # Filled in by :func:`fetch_detail`; all optional because a listing alone
    # cannot supply them.
    poster: str | None = None
    studio: str | None = None
    year: int | None = None
    released: str | None = None
    rating: float | None = None
    duration_minutes: int | None = None
    performers: list[str] = field(default_factory=list)
    also_bought: list[str] = field(default_factory=list)


@dataclass(slots=True)
class StudioRef:
    """A studio as the sitemaps know it, before its page has been read."""

    ae_id: str
    slug: str
    path: str

    # Only the studio page carries these, so they stay unset until it is read.
    name: str | None = None
    title_count: int | None = None


class AdultEmpireError(Exception):
    pass


class AdultEmpireClient:
    """Polite, single-threaded reader for the public browse pages."""

    def __init__(self, base_url: str = BASE_URL, delay: float = REQUEST_DELAY_SECONDS):
        self.base_url = base_url.rstrip("/")
        self.delay = delay
        self._last_request = 0.0

    def _get(self, path: str) -> str:
        # Serialised by construction: sleep out the remainder of the delay
        # rather than sleeping a flat amount, so a slow response is not
        # punished twice.
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

        url = path if path.startswith("http") else f"{self.base_url}{path}"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise AdultEmpireError(f"{url} -> HTTP {exc.code}") from exc
        except Exception as exc:
            raise AdultEmpireError(f"{url} -> {exc}") from exc
        finally:
            self._last_request = time.monotonic()

        if "ageConfirmationButton" in body:
            # Only happens if the user agent is changed to a browser string.
            raise AdultEmpireError(
                f"{url} returned the age/terms interstitial. The crawler "
                f"identifies as a bot precisely to avoid accepting the site's "
                f"terms; do not swap in a browser user agent."
            )

        return body

    def listing(self, name: str, pages: int = 1) -> list[RankedTitle]:
        """Read a ranked listing. Rank is global across pages, starting at 1."""

        if name not in LISTINGS:
            raise AdultEmpireError(f"Unknown listing {name!r}; have {sorted(LISTINGS)}")

        out: list[RankedTitle] = []

        for page in range(1, pages + 1):
            path = LISTINGS[name] + (f"?page={page}" if page > 1 else "")

            try:
                body = self._get(path)
            except AdultEmpireError as exc:
                logger.warning(f"Adult Empire listing {name} page {page} failed: {exc}")
                break

            found = parse_listing(body, name, start_rank=len(out) + 1)

            if not found:
                break

            out.extend(found)

            if len(found) < ITEMS_PER_PAGE:
                break

        return out

    def enrich(self, item: RankedTitle) -> RankedTitle:
        """Fetch the detail page and fill in studio, rating, cast and related.

        Failures are logged and the item returned unchanged: a ranked title
        with no rating is still a ranked title, and one bad page should not
        abort a run of hundreds.
        """

        try:
            body = self._get(item.url)
        except AdultEmpireError as exc:
            logger.debug(f"Adult Empire detail failed for {item.title!r}: {exc}")
            return item

        return parse_detail(body, item)


    # ---------------------------------------------------------------- studios

    def studio_refs(self) -> list[StudioRef]:
        """Every studio the catalogue indexes list, unioned by id.

        ``?letter=all`` is what makes these indexes complete -- without it
        the page renders only its default top slice. The three pages are the
        movie, video and Blu-ray catalogues; a studio usually appears in more
        than one under different slugs, and the first slug seen wins. The
        movie index is read first, so that is the ``-porn-movies`` form,
        which is the catalogue the ranked listings and :func:`parse_listing`
        are built around.
        """

        found: dict[str, StudioRef] = {}

        for page in STUDIO_INDEX_PAGES:
            try:
                body = self._get(page + "?letter=all")
            except AdultEmpireError as exc:
                # One catalogue's index being unavailable should cost its
                # exclusive studios, not the whole directory.
                logger.warning(f"Adult Empire studio index {page} failed: {exc}")
                continue

            for ref in parse_studio_refs(body):
                found.setdefault(ref.ae_id, ref)

        return list(found.values())

    def studio_detail(self, ref: StudioRef) -> StudioRef:
        """Fill in the studio's display name and title count.

        Worth the request: the slug does not round-trip to a name. "roccos"
        and "the-fashionistas" have no apostrophe and no article position to
        recover, so a derived name would be visibly wrong on the exact studios
        a user is most likely to have saved.
        """

        try:
            body = self._get(ref.path)
        except AdultEmpireError as exc:
            logger.debug(f"Adult Empire studio page failed for {ref.slug}: {exc}")
            return ref

        return parse_studio_detail(body, ref)

    def studio_listing(
        self, ref: StudioRef, sort: str, pages: int = 1
    ) -> list[RankedTitle]:
        """A studio's catalogue in one of its ranked orders.

        Not cached. A studio page is one request and the ordering is the whole
        value of it; mirroring two orders for every studio would be twenty
        thousand rows rebuilt weekly to serve pages mostly never opened.
        """

        if sort not in STUDIO_SORTS:
            raise AdultEmpireError(
                f"Unknown studio sort {sort!r}; have {sorted(STUDIO_SORTS)}"
            )

        out: list[RankedTitle] = []

        for page in range(1, pages + 1):
            query = f"?sort={sort}" + (f"&page={page}" if page > 1 else "")

            try:
                body = self._get(ref.path + query)
            except AdultEmpireError as exc:
                logger.warning(
                    f"Adult Empire studio {ref.slug} {sort} page {page}: {exc}"
                )
                break

            found = parse_listing(body, sort, start_rank=len(out) + 1)

            if not found:
                break

            out.extend(found)

            if len(found) < ITEMS_PER_PAGE:
                break

        return out


def parse_studio_refs(html: str) -> list[StudioRef]:
    """Extract studio ids and slugs from a ``?letter=all`` index page.

    Each card links its id twice -- an image and a title, the same pattern
    the product cards use -- so ids are deduplicated here rather than left to
    the caller.
    """

    out: list[StudioRef] = []
    seen: set[str] = set()

    for _href, ae_id, slug in _STUDIO_HREF.findall(html):
        if ae_id in seen:
            continue

        seen.add(ae_id)
        out.append(
            StudioRef(
                ae_id=ae_id,
                slug=slug,
                path=f"/{ae_id}/studio/{slug}.html",
            )
        )

    return out


def parse_studio_detail(html: str, ref: StudioRef) -> StudioRef:
    """Read the display name and title count off a studio page."""

    name = _STUDIO_NAME.search(html)
    count = _STUDIO_COUNT.search(html)

    if name:
        # The headline can carry markup around the name on some studios.
        ref.name = _unescape(re.sub(r"<[^>]+>", "", name.group(1)).strip()) or None

    if count:
        ref.title_count = int(count.group(1).replace(",", ""))

    return ref


def parse_listing(html: str, listing: str, start_rank: int = 1) -> list[RankedTitle]:
    """Extract ranked items from a listing page.

    Rank is position: the page carries no explicit rank number anywhere.
    """

    out: list[RankedTitle] = []
    seen: set[str] = set()
    starts = [(m.start(), m.group(1)) for m in _CARD_START.finditer(html)]

    for index, (offset, product_id) in enumerate(starts):
        if product_id in seen:
            continue

        end = starts[index + 1][0] if index + 1 < len(starts) else len(html)
        block = html[offset:end]
        title_match = _CARD_TITLE.search(block)

        if not title_match:
            continue

        href = title_match.group(1).strip()
        # Listings mix in sex toys and performer cards; only movies carry the
        # -porn-movies slug.
        if not _MOVIE_HREF.match(href):
            continue

        poster = None

        for candidate in _CARD_POSTER.findall(block):
            # Blank placeholder gifs stand in for lazy-loaded covers.
            if "blank" not in candidate:
                poster = large_poster(candidate)
                break

        seen.add(product_id)
        out.append(
            RankedTitle(
                product_id=product_id,
                title=_unescape(title_match.group(2).strip()),
                rank=start_rank + len(out),
                listing=listing,
                url=href,
                poster=poster,
            )
        )

    return out


def large_poster(url: str) -> str:
    """Upgrade a grid thumbnail URL to the large boxcover."""

    return _POSTER_SIZE.sub(r"\1h.jpg", url)


def parse_detail(html: str, item: RankedTitle) -> RankedTitle:
    """Fill a ranked item in from its detail page."""

    # Only when the caller has none. A listing already supplies the title and
    # that is the one the brochure shows; this is for callers that start from
    # a bare product id -- promoting a studio row, which otherwise has no
    # title at all and used to fall back to "Adult Empire 5426".
    if not item.title:
        heading = _DETAIL_TITLE.search(html)

        if heading:
            item.title = _unescape(re.sub(r"<[^>]+>", "", heading.group(1)).strip())

    rating = _RATING.search(html)

    if rating:
        try:
            item.rating = float(rating.group(1))
        except ValueError:
            pass

    studio = _STUDIO.search(html)

    if studio:
        item.studio = _unescape(studio.group(1))

    year = _YEAR.search(html)

    if year:
        item.year = int(year.group(1))

    released = _RELEASED.search(html)

    if released:
        item.released = released.group(1)

    length = _LENGTH.search(html)

    if length and (length.group(1) or length.group(2)):
        hours = int(length.group(1) or 0)
        minutes = int(length.group(2) or 0)
        item.duration_minutes = hours * 60 + minutes

    # Cast links appear in the cast block and again in "also bought"; dedupe
    # while preserving billing order.
    cast: list[str] = []

    for name in _CAST.findall(html):
        clean = _unescape(name)

        if clean and clean not in cast:
            cast.append(clean)

    item.performers = cast

    if not item.poster:
        cover = re.search(
            r'src="(https://[^"]+?/products/[^"]+?\.jpg)"', html
        )

        if cover:
            item.poster = large_poster(cover.group(1))

    anchor = html.find('name="alsobought"')

    if anchor >= 0:
        related: list[str] = []

        for product_id, _slug in _ALSO_BOUGHT.findall(html[anchor : anchor + 12000]):
            if product_id != item.product_id and product_id not in related:
                related.append(product_id)

        item.also_bought = related

    return item


def _unescape(text: str) -> str:
    import html as html_module

    return re.sub(r"\s+", " ", html_module.unescape(text)).strip()
