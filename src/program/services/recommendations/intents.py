"""Named, hand-authored facet expressions -- what a person actually asks for.

"Outdoor", "real plot", "believable" are not tags and never will be. They are
*combinations* of tags, plus a couple of numeric bounds, and the honest way to
serve them is to write those combinations down where they can be read and
corrected rather than to infer them.

This is the deliberate alternative to embedding search over titles, and the
reason is the same one that runs through this codebase: ``ACCEPT_SCORE = 6.0``
refuses rather than guesses, ``assign_provider_id`` writes nothing rather than
the wrong column. A vector index would answer every query confidently, with no
provenance and no way to tell a good answer from a bad one. An intent, by
contrast, can explain itself: *this matched because it has Locations:Beach and
Themes:Parody and runs 96 minutes*.

Defaults ship in :data:`DEFAULT_INTENTS`. Overrides live in a JSON file in the
data directory, merged per-intent by name, so an operator can retune one
without forking the rest -- and so an LLM may be used offline to *draft* an
expression, with the deterministic matcher still deciding.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from loguru import logger

from program.services.recommendations.facets import (
    Facet,
    normalise_value,
    parse_key,
    vocabulary,
)
from program.utils import data_dir_path


@dataclass(slots=True)
class Intent:
    """One named request, as a facet expression.

    ``any`` is the pull, ``all`` the requirement, ``none`` the veto. They are
    separate because they carry different weight: matching more of ``any``
    scores higher, while a single ``none`` hit disqualifies outright -- being
    *partly* the thing someone ruled out is not a near miss, it is a wrong
    answer.
    """

    name: str
    label: str
    description: str = ""
    any: list[str] = field(default_factory=list)
    all: list[str] = field(default_factory=list)
    none: list[str] = field(default_factory=list)
    min_runtime: int | None = None
    max_runtime: int | None = None
    min_year: int | None = None
    max_year: int | None = None
    #: Which engine this intent is meant for. StashDB is scene-shaped and its
    #: corpus is modern amateur/gonzo; the movie corpus is where "plot" lives.
    #: An intent that lies about this returns confident nonsense.
    engines: list[str] = field(default_factory=lambda: ["movies", "scenes"])

    def matches_engine(self, engine: str) -> bool:
        return engine in self.engines

    # --- evaluation ------------------------------------------------------

    def evaluate(
        self,
        facets: Iterable[Facet],
        *,
        runtime: int | None = None,
        year: int | None = None,
    ) -> tuple[float, list[str]] | None:
        """Score a title against this intent, with its reasons.

        Returns ``None`` when the title is *disqualified* -- a ``none`` hit, a
        missing ``all`` term, or a bound it falls outside. That is distinct
        from scoring 0.0, which means "eligible, nothing pulled for it", and
        the two must not be collapsed: a bare list would otherwise fill with
        titles the intent explicitly rules out.
        """

        present = {normalise_value(facet.value) for facet in facets}
        present |= {normalise_value(facet.key) for facet in facets}
        reasons: list[str] = []

        for key in self.none:
            if _present(key, present):
                return None

        for key in self.all:
            if not _present(key, present):
                return None

            reasons.append(key)

        if self.min_runtime is not None and (runtime or 0) < self.min_runtime:
            return None

        if self.max_runtime is not None and runtime is not None and runtime > self.max_runtime:
            return None

        if self.min_year is not None and (year or 0) < self.min_year:
            return None

        if self.max_year is not None and year is not None and year > self.max_year:
            return None

        hits = [key for key in self.any if _present(key, present)]
        reasons.extend(hits)

        if self.any and not hits and not self.all:
            # Nothing pulled and nothing was required: eligible but unwanted.
            # Scored zero rather than rejected, so a rail can still fall back
            # to it when an intent is too narrow for the corpus on hand.
            return 0.0, reasons

        score = len(hits) / len(self.any) if self.any else 1.0

        return score, reasons

    # --- server-side form -------------------------------------------------

    def stashdb_tag_ids(self) -> tuple[list[str], list[str]]:
        """``(included, excluded)`` StashDB tag ids for this intent.

        Only the terms StashDB actually knows survive here; a ``genre:``
        reference the graph has never seen simply is not sent. The caller must
        therefore keep evaluating locally as well -- the server-side filter is
        a narrowing, not the whole judgement.
        """

        included = vocabulary.tag_ids([*self.all, *self.any])
        excluded = vocabulary.tag_ids(self.none)

        return included, excluded


def _present(key: str, present: set[str]) -> bool:
    category, value = parse_key(key)
    value_key = normalise_value(value)

    if category:
        return normalise_value(f"{category}:{value}") in present or value_key in present

    return value_key in present


# ------------------------------------------------------------- the defaults

DEFAULT_INTENTS: list[Intent] = [
    Intent(
        name="real-plot",
        label="Has a real plot",
        description=(
            "Scripted features with characters and a story, not a sequence of "
            "scenes. Runtime is part of the definition: nothing under 80 "
            "minutes sustains a plot."
        ),
        any=[
            "Themes:3rd Person Narrative",
            "Themes:Parody",
            "narrative",
            "character",
            "story",
            "drama",
            "feature",
        ],
        none=["Themes:Amateur", "Themes:Casting", "gonzo", "compilation"],
        min_runtime=80,
        engines=["movies"],
    ),
    Intent(
        name="believable",
        label="Believable",
        description=(
            "Warm rather than performative, and nothing built on coercion or "
            "a power imbalance."
        ),
        any=["Moods:Passion", "Moods:Romance", "Moods:Relaxed", "Moods:Playful", "sensual"],
        none=[
            "Moods:Brutal",
            "Moods:Aggressive",
            "Themes:Blackmail",
            "Themes:Casting",
            "rough",
        ],
    ),
    Intent(
        name="outdoor",
        label="Outdoors",
        description="Somewhere that is not a bedroom or a set.",
        any=[
            "Locations:Outdoors",
            "Locations:Nature",
            "Locations:Beach",
            "Locations:Forest",
            "Locations:Park",
            "Locations:Backyard",
            "Locations:Camping",
            "Locations:Boat",
            "Locations:Poolside",
            "Locations:Garden",
            "Locations:Balcony",
        ],
    ),
    Intent(
        name="artistic",
        label="Shot with care",
        description="Cinematography and mood carrying as much weight as the sex.",
        any=["Moods:Artistic", "Moods:Sultry", "Moods:Sunlit", "Moods:Night", "artistic"],
        none=["Themes:Amateur"],
    ),
    Intent(
        name="comedy",
        label="Funny",
        description="Parody and comedy -- the genre with the most award history behind it.",
        any=["Themes:Parody", "Themes:Comedy", "comedy", "parody"],
        engines=["movies"],
    ),
    Intent(
        name="vintage",
        label="The golden age",
        description=(
            "The 1970s and 1980s feature era, when these were shot on film and "
            "released in cinemas."
        ),
        any=["Themes:1970s", "Themes:1980s", "vintage", "classic"],
        max_year=1989,
        engines=["movies"],
    ),
]

#: An override file may retune any of the above by name, or add its own.
INTENTS_FILENAME = "intents.json"


class IntentLibrary:
    """The shipped defaults, overlaid with the operator's own file."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._intents: dict[str, Intent] | None = None

    @property
    def path(self) -> Path:
        if self._path is not None:
            return self._path

        return data_dir_path / INTENTS_FILENAME

    def _read_overrides(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            # Loud, because a broken override file silently reverting someone
            # to the defaults is exactly the kind of thing that gets debugged
            # for an hour in the wrong place.
            logger.error(f"Could not read {self.path}; using the default intents: {e}")

            return []

        if isinstance(payload, dict):
            payload = payload.get("intents", [])

        return [entry for entry in payload if isinstance(entry, dict)]

    @property
    def intents(self) -> dict[str, Intent]:
        if self._intents is not None:
            return self._intents

        merged = {intent.name: intent for intent in DEFAULT_INTENTS}
        allowed = {f.name for f in Intent.__dataclass_fields__.values()}  # type: ignore[attr-defined]

        for entry in self._read_overrides():
            name = str(entry.get("name") or "").strip()

            if not name:
                continue

            fields = {key: value for key, value in entry.items() if key in allowed}
            base = merged.get(name)

            if base is None:
                fields.setdefault("label", name.replace("-", " ").title())
                merged[name] = Intent(**fields)  # type: ignore[arg-type]

                continue

            for key, value in fields.items():
                setattr(base, key, value)

        self._intents = merged

        return merged

    def get(self, name: str) -> Intent | None:
        return self.intents.get(name)

    def for_engine(self, engine: str) -> list[Intent]:
        return [
            intent for intent in self.intents.values() if intent.matches_engine(engine)
        ]

    def reload(self) -> None:
        self._intents = None


library = IntentLibrary()
