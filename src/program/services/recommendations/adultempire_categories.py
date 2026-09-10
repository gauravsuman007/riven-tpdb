"""Adult Empire's category taxonomy, as the movie corpus's facet source.

The problem this solves, measured rather than assumed: a product page carries
length, production year, studio and cast, and **no genre information at all**.
Every `Label=` on a DVD page is navigation. So a mirrored brochure entry knows
how long a film is and who is in it, and nothing about what it *is* -- which is
why the intent rails that ask about theme or mood ("has a real plot", "shot
with care") had nothing to match on and came back empty.

The genre data exists, but only in the other direction: the site has 507
browsable categories, each a listing of the titles in it. So the index is
built by reading the categories we care about and recording which products
appear in them, rather than by reading products and asking what they are.

    Feature       12,238      Classic Plot   1,836      Romance   1,485
    Parody         1,296      Outdoors       2,368      Comedy      815
    Classic        7,827      Vintage Porn     193      Beach       277

Only the categories the intent library actually names are fetched, and only
the first pages of each: listings are ordered by demand, and the brochure
mirror holds the top-ranked titles, so the overlap is front-loaded. A full
crawl of Feature alone would be 255 requests at the storefront's
one-per-second courtesy delay to catalogue twelve thousand titles we do not
have.

The result is a JSON file, not a table. It is a derived index that can be
rebuilt from the source at any time -- the same reasoning as the 130,547-title
sitemap index -- and keeping it out of the database means no migration and no
risk to the entries themselves.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from program.services.recommendations.adultempire import (
    ITEMS_PER_PAGE,
    AdultEmpireClient,
    AdultEmpireError,
    parse_listing,
)
from program.utils import data_dir_path

#: The taxonomy page. Movies only: the video and sex-toy taxonomies are
#: separate pages with their own category ids, and the brochure mirrors movies.
CATEGORY_INDEX_PAGE = "/browse-porn-movie-categories.html"

INDEX_FILENAME = "adultempire_categories.json"

# One category row: `<a href="/77/category/parody-porn-movies.html"
# title="Parody" ...>Parody</a> &nbsp;<small>(1,296)</small>`. The title
# attribute is read rather than the anchor text because the anchor carries
# whitespace and entities the attribute does not.
_CATEGORY_ROW = re.compile(
    r'href="(/\d+/category/[a-z0-9-]+\.html)"\s+title="([^"]+)"[^>]*>'
    r"[^<]*</a>\s*&nbsp;<small>\(([\d,]+)\)"
)


@dataclass(frozen=True, slots=True)
class Category:
    """One browsable category and how many titles it holds."""

    path: str
    name: str
    count: int


def parse_category_index(html: str) -> list[Category]:
    categories: list[Category] = []

    for path, name, count in _CATEGORY_ROW.findall(html):
        try:
            total = int(count.replace(",", ""))
        except ValueError:
            total = 0

        categories.append(Category(path=path, name=name.strip(), count=total))

    return categories


#: Categories worth indexing, by the storefront's own names. Each is here
#: because an intent asks a question it answers -- "Classic Plot" and "Feature"
#: are the "real plot" signal, "Outdoors"/"Beach"/"Public Sex" the outdoor one,
#: "Classic"/"Vintage Porn" the golden-age one. Adding an intent that needs a
#: new one means adding it here; nothing infers it, deliberately, because a
#: wrong guess here costs a few hundred wasted requests.
DEFAULT_CATEGORIES: tuple[str, ...] = (
    "Feature",
    "Classic Plot",
    "Parody",
    "Comedy",
    "Romance",
    "Couples",
    "Outdoors",
    "Beach",
    "Public Sex",
    "Classic",
    "Vintage Porn",
    "Softcore",
)


class CategoryIndex:
    """Product id -> the categories it is listed under."""

    def __init__(self, client: AdultEmpireClient | None = None) -> None:
        self._client = client
        self._index: dict[str, list[str]] | None = None
        self._fetched_at: float = 0.0
        # A sync is minutes of rate-limited requests. Two of them interleaved
        # would double the load on a storefront that is read one page per
        # second out of courtesy, and produce nothing the single run does not.
        self._lock = threading.Lock()
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def client(self) -> AdultEmpireClient:
        # Built on demand: constructing one opens a session, and the engine
        # imports this module whether or not anything will crawl.
        if self._client is None:
            self._client = AdultEmpireClient()

        return self._client

    @property
    def path(self) -> Path:
        return data_dir_path / INDEX_FILENAME

    # --- persistence -----------------------------------------------------

    def _load(self) -> dict[str, list[str]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            logger.warning(f"Could not read {self.path}; treating it as empty: {e}")

            return {}

        self._fetched_at = float(payload.get("fetched_at", 0.0))
        products = payload.get("products")

        if not isinstance(products, dict):
            return {}

        return {
            str(product): [str(name) for name in names]
            for product, names in products.items()
            if isinstance(names, list)
        }

    def _store(self, index: dict[str, list[str]]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"fetched_at": time.time(), "products": index}),
                encoding="utf-8",
            )
        except OSError as e:
            logger.error(f"Could not write the category index to {self.path}: {e}")

    @property
    def index(self) -> dict[str, list[str]]:
        if self._index is None:
            self._index = self._load()

        return self._index

    @property
    def empty(self) -> bool:
        return not self.index

    @property
    def fetched_at(self) -> float:
        # Reading the property populates `_fetched_at` as a side effect of the
        # load, so it has to be touched before the timestamp is trusted.
        _ = self.index

        return self._fetched_at

    # --- the crawl -------------------------------------------------------

    def categories(self) -> list[Category]:
        """The whole taxonomy, one request."""

        try:
            return parse_category_index(self.client._get(CATEGORY_INDEX_PAGE))  # noqa: SLF001
        except AdultEmpireError as e:
            logger.warning(f"Could not read the Adult Empire category index: {e}")

            return []

    def sync(
        self,
        names: tuple[str, ...] | list[str] = DEFAULT_CATEGORIES,
        pages_per_category: int = 20,
    ) -> dict[str, int]:
        """Index the named categories. Returns titles found per category.

        Additive: a category that could not be read this time keeps whatever
        it contributed last time, because a partial crawl overwriting a good
        index with a worse one is indistinguishable from the site having
        changed.
        """

        with self._lock:
            if self._running:
                logger.debug("A category sync is already running; not starting another.")

                return {}

            self._running = True

        try:
            return self._sync(names, pages_per_category)
        finally:
            self._running = False

    def _sync(
        self,
        names: tuple[str, ...] | list[str],
        pages_per_category: int,
    ) -> dict[str, int]:
        taxonomy = {
            category.name.casefold(): category for category in self.categories()
        }

        if not taxonomy:
            return {}

        index = dict(self.index)
        found: dict[str, int] = {}

        for name in names:
            category = taxonomy.get(name.casefold())

            if category is None:
                logger.warning(
                    f"Adult Empire has no category named {name!r}; skipping it."
                )

                continue

            products = self._crawl(category, pages_per_category)

            if not products:
                continue

            for product_id in products:
                listed = index.setdefault(product_id, [])

                if category.name not in listed:
                    listed.append(category.name)

            found[category.name] = len(products)

        if not found:
            return {}

        self._index = index
        self._store(index)
        logger.success(
            f"Indexed {sum(found.values())} Adult Empire titles "
            f"across {len(found)} categories."
        )

        return found

    def _crawl(self, category: Category, pages: int) -> list[str]:
        products: list[str] = []

        for page in range(1, pages + 1):
            query = "?sort=bestseller" + (f"&page={page}" if page > 1 else "")

            try:
                body = self.client._get(category.path + query)  # noqa: SLF001
            except AdultEmpireError as e:
                logger.warning(
                    f"Adult Empire category {category.name!r} page {page}: {e}"
                )

                break

            titles = parse_listing(body, category.name, start_rank=len(products) + 1)

            if not titles:
                break

            products.extend(title.product_id for title in titles)

            # A short page is the last page. Asking for the next one returns
            # the first again on this site, which would loop.
            if len(titles) < ITEMS_PER_PAGE:
                break

        return products

    # --- use -------------------------------------------------------------

    def categories_for(self, product_id: str | None) -> list[str]:
        if not product_id:
            return []

        return self.index.get(str(product_id), [])


#: Shared, because it holds a lazily-loaded index that would otherwise be
#: re-read from disk on every scored entry.
category_index = CategoryIndex()
