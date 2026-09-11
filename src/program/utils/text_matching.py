"""Pure text helpers for comparing a title against a release or video name.

Lives here rather than in ``program.services.scrapers.adult_matching`` because
importing that module executes the whole scrapers package, which pulls in the
settings model, the DI container and the ORM. The direct-site ranker needs
nothing but string handling, and a comparison function that cannot be imported
without a database is a function that cannot be tested quickly either.

``adult_matching`` re-exports these, so its own callers are unaffected and the
noise list stays a single definition.
"""

from __future__ import annotations

import re


#: Tokens that carry no identifying information in a release title: format,
#: encoding and structural words. Distinct from everyday English filler, which
#: the direct-site ranker strips separately.
NOISE = frozenset({
    "xxx", "the", "a", "an", "and", "of", "in", "on", "at", "to", "for", "with",
    "vol", "volume", "part", "pt", "scene", "episode", "ep", "featuring", "feat",
    "web", "dl", "webrip", "bluray", "brrip", "hdrip", "dvdrip", "dvd", "rip",
    "mp4", "mkv", "avi", "wmv", "hevc", "x264", "x265", "h264", "h265", "aac",
    "1080p", "2160p", "720p", "480p", "360p", "540p", "4k", "uhd", "sd", "hd",
    "uncen", "decen", "uncensored", "censored", "split", "scenes", "new",
})

# Series instalment number: "Vol. 3", "Volume 10", "Part 2", or a bare trailing
# number as in "Daddy Issues 8". Adult series reuse one name across many
# volumes, so getting this wrong hands over a different film entirely.
_VOLUME = re.compile(r"\b(?:vol(?:ume)?|part|pt)\.?\s*(\d{1,3})\b", re.I)
_TRAILING_NUMBER = re.compile(r"\b(\d{1,3})\s*$")

#: Small numbers written as words. Series titles number their volumes in
#: digits, but seasons are routinely spelled out -- "Girlcore Season Two" --
#: and a season written as a word is exactly as much a season number as one
#: written as a digit.
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

# Season number: "Season 2", "Season Two", "S2". Deliberately a SEPARATE axis
# from the volume number -- "Girlcore Season Two: Volume 1" states both, and
# reading only the volume made it agree with "Girlcore: Season 1".
_SEASON = re.compile(
    r"\bs(?:eason)?\.?\s*(\d{1,2}|" + "|".join(_NUMBER_WORDS) + r")\b", re.I
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
    return [w for w in words if w and w not in NOISE]


def extract_volume(text: str, *, trailing_number: bool = True) -> int | None:
    """The instalment number a title refers to, if it states one.

    `trailing_number` controls whether a bare number at the end counts. It must
    for an item title, where "Daddy Issues 8" states the instalment that way and
    nowhere else. It must not for a scraped video title, where the last number
    is as likely to be a scene number or a performer count -- reading the 2 in
    "Step daddy Issues 8 Sc 2" as the volume rejects an exact match.
    """

    if not text:
        return None

    match = _VOLUME.search(text)

    if match:
        return int(match.group(1))

    if not trailing_number:
        return None

    # "Girlcore Season 2" ends in a number that is the SEASON, not a volume.
    # Reading it as one made a title conflict with itself: the same work
    # written "Season Two" states no trailing number at all, so one side got
    # a volume and the other did not.
    trailing = _TRAILING_NUMBER.search(_SEASON.sub(" ", text).strip())

    if trailing:
        value = int(trailing.group(1))

        # Years are not volume numbers.
        if not (1900 <= value <= 2100):
            return value

    return None


def extract_season(text: str) -> int | None:
    """The season a title refers to, if it states one.

    Separate from :func:`extract_volume` on purpose. A title can state both --
    "Girlcore Season Two: Volume 1" -- and a matcher that reads only the volume
    concludes that it agrees with "Girlcore: Season 1", which is a different
    season of the same series and so a different work. Measured against the
    live catalogue, that was exactly the wrong match the request button made.
    """

    if not text:
        return None

    match = _SEASON.search(text)

    if not match:
        return None

    value = match.group(1).lower()

    return int(value) if value.isdigit() else _NUMBER_WORDS[value]
