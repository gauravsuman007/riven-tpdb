"""Relevance matching for adult releases.

Why this exists
---------------
RTN ranks mainstream quality markers -- BluRay, DTS-HD, remux -- which adult
releases rarely carry, so its rank is close to noise here. An earlier attempt
to compensate accepted any release RTN flagged ``adult``, which turned out to
be far worse than it sounds: a search for "Daddy Issues 8" (Diabolic Video)
returned 180 unrelated JAV releases that merely contained the word "daddy",
every one of them adult-flagged and therefore accepted.

What actually identifies an adult release
-----------------------------------------
Adult scene naming is highly conventional:

    PureTaboo 19 06 11 Whitney Wright Alpha Male XXX 2160p MP4-KTR
    FamilySinners.22.02.18.Ana.Foxxx.Family.Cheaters.XXX.1080p.HEVC
    [SweetSinner] Paige Owens - Family's Dirty Secrets Scene 3

which is ``{site} {date} {performers} {title} XXX {quality}``. This is the
grammar Whisparr matches on, and its central insight is that **site plus date
is the identity of a scene**, not the title -- release titles routinely differ
from the site's official title, while the site/date pair is near-unique.

TPDB gives us the site name, the performer list and the release date for every
title. None of that was being used. This module scores a release against all of
it, and requires corroborating evidence rather than a single weak signal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

# Tokens that carry no identifying information in an adult release title.
_NOISE = frozenset({
    "xxx", "the", "a", "an", "and", "of", "in", "on", "at", "to", "for", "with",
    "vol", "volume", "part", "pt", "scene", "episode", "ep", "featuring", "feat",
    "web", "dl", "webrip", "bluray", "brrip", "hdrip", "dvdrip", "dvd", "rip",
    "mp4", "mkv", "avi", "wmv", "hevc", "x264", "x265", "h264", "h265", "aac",
    "1080p", "2160p", "720p", "480p", "360p", "540p", "4k", "uhd", "sd", "hd",
    "uncen", "decen", "uncensored", "censored", "split", "scenes", "new",
})

_YEAR = re.compile(r"\b(19|20)\d{2}\b")

# Mainstream episodic markers. An adult title is never S03E09, so their
# presence is positive evidence that a release belongs to something else.
_EPISODIC = re.compile(r"\bS\d{1,2}\s?E\d{1,3}\b|\b\d{1,2}x\d{2}\b", re.I)

# Series instalment number: "Vol. 3", "Volume 10", "Part 2", or a bare trailing
# number as in "Daddy Issues 8". Adult series reuse one name across many
# volumes, so getting this wrong hands over a different film entirely.
_VOLUME = re.compile(r"\b(?:vol(?:ume)?|part|pt)\.?\s*(\d{1,3})\b", re.I)
_TRAILING_NUMBER = re.compile(r"\b(\d{1,3})\s*$")


def extract_volume(text: str) -> int | None:
    """The instalment number a title refers to, if it states one."""

    if not text:
        return None

    match = _VOLUME.search(text)

    if match:
        return int(match.group(1))

    # Only trust a bare trailing number on the *item* title ("Daddy Issues 8"),
    # never mid-string, where it is usually a year or a resolution.
    trailing = _TRAILING_NUMBER.search(text.strip())

    if trailing:
        value = int(trailing.group(1))

        # Years are not volume numbers.
        if not (1900 <= value <= 2100):
            return value

    return None

# Scene dates appear as 22.02.18 / 22 02 18 / 2022.02.18, occasionally reordered.
_DATE_PATTERNS = (
    re.compile(r"\b(20\d{2})[.\-_ ](\d{2})[.\-_ ](\d{2})\b"),
    re.compile(r"\b(\d{2})[.\-_ ](\d{2})[.\-_ ](\d{2})\b"),
)


def normalise(text: str) -> str:
    """Lowercase and strip everything that is not a letter or digit.

    Site names are written every possible way -- "Pure Taboo", "PureTaboo",
    "pure-taboo" -- so comparisons happen in this collapsed space.
    """

    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def tokenise(text: str) -> list[str]:
    """Split into meaningful lowercase word tokens, dropping release noise."""

    words = re.split(r"[^a-zA-Z0-9]+", (text or "").lower())

    # Digits are kept: the volume number is often the only thing separating
    # "Daddy Issues 8" from "Daddy Issues", and dropping it made every
    # instalment of a series look like every other one.
    return [w for w in words if w and w not in _NOISE]


def extract_dates(raw_title: str) -> list[tuple[int, int, int]]:
    """Candidate (year, month, day) triples found in a release title.

    Two-digit years are assumed to be 2000s: adult scene releases do not
    predate that, and a 19xx reading would produce nonsense matches.
    """

    found = list[tuple[int, int, int]]()

    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(raw_title):
            a, b, c = (int(g) for g in match.groups())
            year = a if a > 100 else 2000 + a

            if 1 <= b <= 12 and 1 <= c <= 31:
                found.append((year, b, c))

    return found


@dataclass
class MatchEvidence:
    """What was found linking a release to a title, and how strong it is."""

    site: bool = False
    date: bool = False
    performers: int = 0
    title_ratio: float = 0.0
    year: bool = False
    adult_flag: bool = False
    episodic: bool = False
    volume_conflict: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        """Weighted confidence, roughly 0-10.

        Site and date dominate because together they identify a scene. Title
        overlap is deliberately worth less than one performer: unrelated
        releases share common words like "daddy" or "family" constantly,
        whereas a performer name appearing by chance is rare.
        """

        total = 0.0

        if self.site:
            total += 3.0
        if self.date:
            total += 3.0

        total += min(self.performers, 3) * 1.5
        total += self.title_ratio * 2.0

        if self.year:
            total += 0.5

        return total

    @property
    def accepted(self) -> bool:
        """Whether the evidence is enough to believe this is the same title.

        Every accepting branch needs two independent signals. One signal alone
        is what produced the JAV flood: those releases matched a title word and
        nothing else.
        """

        # An episode number means this belongs to a mainstream series --
        # "Shrinking S03E09 Daddy Issues" is not an adult release however
        # perfectly its episode name matches.
        if self.episodic:
            return False

        # Both sides name an instalment and they disagree: this is a different
        # film in the same series, however well everything else lines up.
        if self.volume_conflict:
            return False

        # The scene identity: site plus when it was published.
        if self.site and self.date:
            return True

        # Site plus a named performer, or site plus most of the title.
        if self.site and (self.performers or self.title_ratio >= 0.5):
            return True

        # No site in the name, but the cast and the title both line up.
        if self.performers and self.title_ratio >= 0.5:
            return True

        # Two or more performers is itself corroboration -- scene compilations
        # often omit the studio.
        if self.performers >= 2 and self.title_ratio > 0:
            return True

        # Last resort for compilations released under exactly the TPDB title
        # with no other metadata in the name. Gated on the release actually
        # parsing as adult, because a bare title match is otherwise how
        # mainstream films with the same name get in.
        if self.adult_flag and self.title_ratio >= 0.9:
            return True

        return False


def evaluate(
    raw_title: str,
    *,
    item_title: str | None,
    site_name: str | None,
    performers: list[str] | None,
    aired_at: datetime | None,
    is_adult_release: bool = False,
) -> MatchEvidence:
    """Score one release against the TPDB metadata for a title."""

    evidence = MatchEvidence()
    evidence.adult_flag = bool(is_adult_release)
    evidence.episodic = bool(_EPISODIC.search(raw_title or ""))
    flat = normalise(raw_title)

    wanted_volume = extract_volume(item_title or "")

    if wanted_volume is not None:
        release_volume = extract_volume(raw_title)

        if release_volume is not None and release_volume != wanted_volume:
            evidence.volume_conflict = True
            evidence.reasons.append(f"volume:{release_volume}!={wanted_volume}")

    # --- site -------------------------------------------------------------
    if site_name:
        site_flat = normalise(site_name)

        if site_flat and site_flat in flat:
            evidence.site = True
            evidence.reasons.append(f"site:{site_name}")
        else:
            # "Family Sinners" also ships as "FamilySinners" and, on some
            # indexers, as "Family.Sinners" -- already handled -- but a
            # single-word contraction like "PureTaboo" -> "Taboo" is too
            # weak to count, so only the full collapsed form is accepted.
            pass

    # --- date -------------------------------------------------------------
    if aired_at:
        target = (aired_at.year, aired_at.month, aired_at.day)

        for candidate in extract_dates(raw_title):
            if candidate == target:
                evidence.date = True
                evidence.reasons.append("date")
                break

        if _YEAR.search(raw_title) and str(aired_at.year) in raw_title:
            evidence.year = True

    # --- performers -------------------------------------------------------
    for performer in performers or []:
        performer_flat = normalise(performer)

        # Require the whole name. Surnames alone ("Wright", "Reid") collide
        # with ordinary words and with unrelated performers.
        if len(performer_flat) >= 6 and performer_flat in flat:
            evidence.performers += 1
            evidence.reasons.append(f"performer:{performer}")

    # --- title ------------------------------------------------------------
    wanted = tokenise(item_title or "")

    if wanted:
        # Compare token to token, not token to the flattened string. Substring
        # matching let the "8" of "Daddy Issues 8" match the "08" inside a
        # release date ("BrattySis 25 08 01 ..."), scoring a perfect title hit
        # for an unrelated studio's scene.
        release_tokens = set(tokenise(raw_title))
        present = sum(1 for token in wanted if token in release_tokens)
        evidence.title_ratio = present / len(wanted)

        if evidence.title_ratio:
            evidence.reasons.append(f"title:{present}/{len(wanted)}")

    return evidence
