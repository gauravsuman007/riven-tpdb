"""Scoring a TPDB search result against a known award entry.

This is the opposite direction to :mod:`program.services.scrapers.adult_matching`,
which scores a release *filename* against TPDB metadata. Here both sides are
catalogue records: an award entry (title, studio, year, cast) against a TPDB
movie or scene. The signals are therefore cleaner, and the bar is set higher --
a wrong match silently puts the wrong film in a curated collection, which is
worse than leaving the entry unmatched and visible as a gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from program.utils.text_matching import extract_volume, normalise, tokenise

# An entry must clear this to be accepted. Tuned so that a title match alone is
# never enough: the maximum from title similarity is 5.0.
ACCEPT_SCORE = 6.0

# Below this the titles are too different to consider whatever else agrees.
MIN_TITLE_RATIO = 0.6


def title_ratio(left: str, right: str) -> float:
    """Token overlap, normalised by the shorter side.

    Normalising by the shorter side means "Strip" matching inside
    "Strip: Director's Cut" still scores well, which is the common shape of a
    TPDB title against an award listing.
    """

    a, b = set(tokenise(left)), set(tokenise(right))

    if not a or not b:
        return 0.0

    return len(a & b) / min(len(a), len(b))


@dataclass(slots=True)
class Match:
    """How well one TPDB candidate fits an award entry."""

    tpdb_id: str
    kind: str
    title: str
    poster: str | None = None
    title_ratio: float = 0.0
    studio: bool = False
    year_delta: int | None = None
    performers: int = 0
    volume_conflict: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        """Weighted confidence, roughly 0-10.

        Studio is the strongest single signal because award listings name the
        studio explicitly and TPDB records it as the site. Cast is next: two
        performers agreeing on the same title is close to conclusive.
        """

        if self.volume_conflict:
            return 0.0

        total = self.title_ratio * 5.0

        if self.studio:
            total += 3.0

        total += min(self.performers, 2) * 1.5

        # The award year is the ceremony year; the work is from the year before,
        # and TPDB dates can drift by a release window either way.
        if self.year_delta is not None and self.year_delta <= 1:
            total += 1.0
        elif self.year_delta is not None and self.year_delta <= 2:
            total += 0.5

        return total

    @property
    def accepted(self) -> bool:
        if self.volume_conflict or self.title_ratio < MIN_TITLE_RATIO:
            return False

        return self.score >= ACCEPT_SCORE


def evaluate_candidate(
    *,
    entry_title: str,
    entry_studio: str | None,
    entry_year: int | None,
    entry_performers: list[str] | None,
    tpdb_id: str,
    tpdb_kind: str,
    tpdb_title: str | None,
    tpdb_site: str | None,
    tpdb_date: str | None,
    tpdb_performers: list[str] | None,
    tpdb_poster: str | None = None,
    year_offset: int = 1,
) -> Match:
    """Score one TPDB record against one catalogue entry.

    ``year_offset`` is subtracted from ``entry_year`` to get the year the work
    is expected to have been released. It defaults to 1 because an award
    ceremony honours the previous year's output; pass 0 when ``entry_year`` is
    already the release year, as it is for a storefront listing.
    """

    match = Match(
        tpdb_id=tpdb_id,
        kind=tpdb_kind,
        title=tpdb_title or "",
        poster=tpdb_poster,
    )

    match.title_ratio = title_ratio(entry_title, tpdb_title or "")

    if match.title_ratio:
        match.reasons.append(f"title:{match.title_ratio:.2f}")

    # A numbered instalment that disagrees is a different film, however well
    # the rest lines up -- "Anal Savages 11" is not "Anal Savages 3".
    wanted = extract_volume(entry_title)
    found = extract_volume(tpdb_title or "")

    if wanted is not None and found is not None and wanted != found:
        match.volume_conflict = True
        match.reasons.append(f"volume:{found}!={wanted}")

    if entry_studio and tpdb_site:
        # Award listings write "Girlcore/Adult Time/Pulse"; any component
        # matching the TPDB site is enough.
        site = normalise(tpdb_site)
        parts = [normalise(p) for p in entry_studio.replace("/", ",").split(",")]

        if site and any(p and (p in site or site in p) for p in parts if len(p) > 2):
            match.studio = True
            match.reasons.append(f"studio:{tpdb_site}")

    if entry_performers and tpdb_performers:
        wanted_cast = {normalise(p) for p in entry_performers if p}
        found_cast = {normalise(p) for p in tpdb_performers if p}
        overlap = wanted_cast & found_cast
        match.performers = len(overlap)

        if overlap:
            match.reasons.append(f"cast:{len(overlap)}")

    if entry_year and tpdb_date:
        try:
            released = datetime.fromisoformat(tpdb_date[:10]).year
        except ValueError:
            released = None

        if released:
            match.year_delta = abs(released - (entry_year - year_offset))
            match.reasons.append(f"year:{released}")

    return match


def best_match(candidates: list[Match]) -> Match | None:
    """The highest-scoring accepted candidate, if any.

    Ties are broken by title similarity so that, between two records from the
    same studio, the closer title wins.
    """

    accepted = [c for c in candidates if c.accepted]

    if not accepted:
        return None

    return max(accepted, key=lambda c: (c.score, c.title_ratio))
