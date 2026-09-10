"""A typed vocabulary for what a title *is*.

``MediaItem.genres`` is one flat list mixing kinds of fact: the live library
has ``blowjob``, ``narrative`` and ``brown hair`` in the same bag. Nothing can
act on that, because nothing knows ``narrative`` is a *different kind of
statement* from ``brown hair`` -- and ``narrative`` is precisely the "has a
real plot" signal someone wants to search by.

A :class:`Facet` restores the kind. StashDB's tag graph is adopted as the
canonical vocabulary because it is already curated and already grouped --
``SCENE / Locations``, ``SCENE / Moods``, ``ACTION / Acts`` and so on -- and
because it is the one source that can *also* answer a query in those terms
server-side. Every other provider (TPDB genres, Adult Empire categories, award
category names) is normalised into the same shape by alias, so an intent can
be expressed once and evaluated against any of them.

Two deliberate limits:

    * Normalisation **refuses rather than guesses**. A string that matches no
      alias and no ingested tag comes back as an ``UNKNOWN`` category facet,
      not as a plausible-looking ``Moods:`` one. Same stance as
      ``assign_provider_id``: a wrong facet is worse than an absent one,
      because it is invisible.
    * The tag graph is *ingested*, never invented. Until StashDB has been
      read, only the built-in aliases exist -- which is enough for the local
      engines and not enough for the scene engine, and the two report that
      difference rather than papering over it.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from loguru import logger

from program.apis.stashdb_api import StashdbApi, StashdbApiError
from program.settings import settings_manager

# --------------------------------------------------------------- the shape

#: Facet kinds, as StashDB groups them. ``GENRE`` is ours: it holds vocabulary
#: that arrives already flat from a provider with no category of its own, so
#: it stays usable without pretending to a precision it does not have.
KIND_SCENE = "SCENE"
KIND_PEOPLE = "PEOPLE"
KIND_ACTION = "ACTION"
KIND_GENRE = "GENRE"

#: The category a value lands in when nothing recognises it. Kept rather than
#: dropped: an unknown facet is still an exact string that two titles can
#: share, and it is the input to widening the alias table later.
UNKNOWN = "Unknown"


@dataclass(frozen=True, slots=True)
class Facet:
    """One statement about a title, with its kind retained.

    ``kind`` is the group (SCENE / PEOPLE / ACTION / GENRE), ``category`` the
    sub-group inside it (Locations, Moods, Themes, Acts...) and ``value`` the
    tag itself. Frozen and hashable so a title's facets are a set.
    """

    kind: str
    category: str
    value: str

    @property
    def key(self) -> str:
        """``Category:Value`` -- how an intent expression names a facet."""

        return f"{self.category}:{self.value}"

    def __str__(self) -> str:
        return self.key


def parse_key(key: str) -> tuple[str | None, str]:
    """Split an intent's ``Category:Value`` (or bare ``value``) reference.

    Returns ``(category or None, value)``. A bare value matches on value
    alone, which is what makes ``genre:narrative`` and ``Themes:Parody``
    expressible side by side in the same intent.
    """

    key = key.strip().strip('"')

    if ":" in key:
        category, _, value = key.partition(":")

        return category.strip() or None, value.strip().strip('"')

    return None, key


# ------------------------------------------------------------- the aliases

# Provider vocabulary that is genuinely the same fact under a different name.
# Only entries verified against the live library and the StashDB tag list are
# here; a guess belongs in the intent file, where it is visible, not buried in
# a normalisation table.
_ALIASES: dict[str, tuple[str, str]] = {
    # The "real plot" cluster. These arrive from TPDB/Adult Empire flat and
    # are the reason this module exists.
    "narrative": (KIND_SCENE, "Themes"),
    "story": (KIND_SCENE, "Themes"),
    "storyline": (KIND_SCENE, "Themes"),
    "feature": (KIND_SCENE, "Themes"),
    "character": (KIND_SCENE, "Themes"),
    "parody": (KIND_SCENE, "Themes"),
    "drama": (KIND_SCENE, "Themes"),
    "comedy": (KIND_SCENE, "Themes"),
    "amateur": (KIND_SCENE, "Themes"),
    "casting": (KIND_SCENE, "Themes"),
    "cheating": (KIND_SCENE, "Themes"),
    "vignette": (KIND_SCENE, "Themes"),
    "gonzo": (KIND_SCENE, "Themes"),
    # Moods.
    "romance": (KIND_SCENE, "Moods"),
    "romantic": (KIND_SCENE, "Moods"),
    "passion": (KIND_SCENE, "Moods"),
    "sensual": (KIND_SCENE, "Moods"),
    "artistic": (KIND_SCENE, "Moods"),
    "softcore": (KIND_SCENE, "Moods"),
    "playful": (KIND_SCENE, "Moods"),
    "hardcore": (KIND_SCENE, "Moods"),
    "aggressive": (KIND_SCENE, "Moods"),
    "rough": (KIND_SCENE, "Moods"),
    # Locations.
    "outdoor": (KIND_SCENE, "Locations"),
    "outdoors": (KIND_SCENE, "Locations"),
    "nature": (KIND_SCENE, "Locations"),
    "beach": (KIND_SCENE, "Locations"),
    "forest": (KIND_SCENE, "Locations"),
    "pool": (KIND_SCENE, "Locations"),
    "poolside": (KIND_SCENE, "Locations"),
    "public": (KIND_SCENE, "Locations"),
    "office": (KIND_SCENE, "Locations"),
    "car": (KIND_SCENE, "Locations"),
}

#: Alias for a whole *category*, not a value: the string on the left is what a
#: provider calls the category, the right is ours. Used when a provider does
#: hand over grouped vocabulary (StashDB does).
_CATEGORY_ALIASES: dict[str, str] = {
    "theme": "Themes",
    "themes": "Themes",
    "location": "Locations",
    "locations": "Locations",
    "mood": "Moods",
    "moods": "Moods",
    "act": "Acts",
    "acts": "Acts",
    "role": "Roles",
    "roles": "Roles",
    "relation": "Relations",
    "relations": "Relations",
    "group makeup": "Group Makeup",
    "orientation": "Orientation",
    "motivation": "Motivations",
    "motivations": "Motivations",
}

_PUNCTUATION = re.compile(r"[^a-z0-9]+")


def normalise_value(value: str) -> str:
    """Fold a raw tag string to its comparison form."""

    return _PUNCTUATION.sub(" ", value.strip().lower()).strip()


# -------------------------------------------------------- the StashDB graph

_TAG_QUERY = """
query Tags($page: Int!, $per_page: Int!) {
    queryTags(input: { page: $page, per_page: $per_page }) {
        count
        tags {
            id
            name
            aliases
            category { id name group }
        }
    }
}
"""

#: StashDB will serve 100 tags per page and there are around three thousand of
#: them, so a full ingest is ~30 calls. Run rarely; the graph is curated and
#: barely moves.
_TAGS_PER_PAGE = 100
_MAX_PAGES = 60


@dataclass(slots=True)
class TagGraph:
    """StashDB's tag vocabulary, as ingested.

    ``by_value`` maps a normalised tag name (and every alias StashDB records
    for it) to the facet it denotes; ``ids`` maps that same normalised name to
    the StashDB tag UUID, which is what ``queryScenes`` filters on. Both are
    needed: the first makes an intent evaluable locally, the second makes it
    evaluable server-side.
    """

    by_value: dict[str, Facet]
    ids: dict[str, str]
    fetched_at: float = 0.0

    @property
    def empty(self) -> bool:
        return not self.by_value

    def categories(self) -> dict[str, int]:
        """How many tags sit in each category. The shape of the vocabulary."""

        counts: dict[str, int] = {}

        for facet in set(self.by_value.values()):
            counts[f"{facet.kind} / {facet.category}"] = (
                counts.get(f"{facet.kind} / {facet.category}", 0) + 1
            )

        return counts


_EMPTY_GRAPH = TagGraph(by_value={}, ids={})


class FacetVocabulary:
    """The alias table plus whatever of StashDB's graph has been ingested.

    Deliberately usable with no StashDB at all: the built-in aliases are what
    the local (movie) engine runs on, and they do not depend on a key being
    configured. StashDB widens the vocabulary and is the only thing that can
    supply tag ids.
    """

    def __init__(self, api: StashdbApi | None = None) -> None:
        self.api = api or StashdbApi()
        self._graph: TagGraph | None = None

    # --- persistence ---------------------------------------------------

    @property
    def cache_path(self) -> Path:
        return Path(settings_manager.settings.stashdb.cache_dir) / "tag_graph.json"

    def _load(self) -> TagGraph:
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _EMPTY_GRAPH

        try:
            by_value = {
                value: Facet(**facet) for value, facet in payload["by_value"].items()
            }
        except (KeyError, TypeError):
            logger.debug("The cached StashDB tag graph is unreadable; ignoring it.")

            return _EMPTY_GRAPH

        return TagGraph(
            by_value=by_value,
            ids=dict(payload.get("ids", {})),
            fetched_at=float(payload.get("fetched_at", 0.0)),
        )

    def _store(self, graph: TagGraph) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(
                    {
                        "fetched_at": graph.fetched_at,
                        "ids": graph.ids,
                        "by_value": {
                            value: {
                                "kind": facet.kind,
                                "category": facet.category,
                                "value": facet.value,
                            }
                            for value, facet in graph.by_value.items()
                        },
                    }
                ),
                encoding="utf-8",
            )
        except OSError as e:
            # A graph that cannot be cached is a slower start, not a failure.
            logger.debug(f"Could not cache the StashDB tag graph: {e}")

    @property
    def graph(self) -> TagGraph:
        if self._graph is None:
            self._graph = self._load()

        return self._graph

    # --- ingest ---------------------------------------------------------

    def ingest(self, force: bool = False) -> TagGraph:
        """Read StashDB's whole tag graph and cache it.

        Returns whatever graph is in force afterwards -- including the cached
        one when StashDB is unconfigured or unreachable, because a stale
        vocabulary is far better than none and a provider without a key has
        not failed, it has not been tried.
        """

        if not force and not self.graph.empty:
            return self.graph

        if not self.api.configured:
            logger.debug("StashDB is not configured; keeping the built-in vocabulary.")

            return self.graph

        by_value: dict[str, Facet] = {}
        ids: dict[str, str] = {}
        page = 1

        while page <= _MAX_PAGES:
            try:
                data = self.api._query(  # noqa: SLF001 - the client's one door
                    _TAG_QUERY, {"page": page, "per_page": _TAGS_PER_PAGE}
                )
            except StashdbApiError as e:
                logger.warning(f"StashDB tag ingest stopped at page {page}: {e}")

                break

            result = data.get("queryTags") or {}
            tags = result.get("tags") or []

            if not tags:
                break

            for tag in tags:
                self._absorb(tag, by_value, ids)

            if len(tags) < _TAGS_PER_PAGE:
                break

            page += 1

        if not by_value:
            return self.graph

        graph = TagGraph(by_value=by_value, ids=ids, fetched_at=time.time())
        self._graph = graph
        self._store(graph)
        logger.success(f"Ingested {len(ids)} StashDB tags into the facet vocabulary.")

        return graph

    @staticmethod
    def _absorb(
        tag: dict[str, Any], by_value: dict[str, Facet], ids: dict[str, str]
    ) -> None:
        name = str(tag.get("name") or "").strip()
        tag_id = str(tag.get("id") or "").strip()

        if not name or not tag_id:
            return

        category = tag.get("category") or {}
        kind = str(category.get("group") or KIND_GENRE).upper()
        category_name = str(category.get("name") or UNKNOWN).strip()
        category_name = _CATEGORY_ALIASES.get(category_name.lower(), category_name)

        facet = Facet(kind=kind, category=category_name, value=name)

        # The tag's own name and every alias StashDB records for it point at
        # the same facet. Aliases are why "Outdoors" and "Outside" do not need
        # to both be guessed at here.
        for label in [name, *(tag.get("aliases") or [])]:
            key = normalise_value(str(label))

            if not key:
                continue

            by_value.setdefault(key, facet)
            ids.setdefault(key, tag_id)

    # --- use -------------------------------------------------------------

    def facet(self, value: str) -> Facet:
        """Normalise one raw provider string into a facet.

        Never returns ``None``: an unrecognised value becomes a ``GENRE /
        Unknown`` facet carrying the original string. That keeps it comparable
        between titles without claiming to know what kind of fact it is.
        """

        key = normalise_value(value)

        if not key:
            return Facet(KIND_GENRE, UNKNOWN, value.strip())

        known = self.graph.by_value.get(key)

        if known is not None:
            return known

        alias = _ALIASES.get(key)

        if alias is not None:
            kind, category = alias

            return Facet(kind, category, value.strip())

        return Facet(KIND_GENRE, UNKNOWN, value.strip())

    def facets(self, values: Iterable[str] | None) -> set[Facet]:
        return {self.facet(value) for value in (values or []) if str(value).strip()}

    def tag_id(self, key: str) -> str | None:
        """The StashDB UUID for an intent's facet reference, if it has one."""

        _, value = parse_key(key)

        return self.graph.ids.get(normalise_value(value))

    def tag_ids(self, keys: Iterable[str]) -> list[str]:
        """Every resolvable tag id for these references, order preserved."""

        found: list[str] = []

        for key in keys:
            tag_id = self.tag_id(key)

            if tag_id and tag_id not in found:
                found.append(tag_id)

        return found


#: One shared vocabulary. It holds a lazily-loaded cache, so constructing it
#: per request would re-read the graph from disk on every call.
vocabulary = FacetVocabulary()
