"""Puts the files of one release into the order a viewer expects.

Import-free on purpose, for the same reason `entry_selection` is: the only
caller is `MediaItem.media_parts`, and `item.py` cannot be imported in a test
without a database. It sits here rather than beside `entry_selection` because
importing anything from the downloaders package pulls in `MediaItem` itself.
Everything here works on any object carrying `original_filename` and
`file_size`.

The rules were measured over all 251 torrents in the TorBox account
(2026-09-19); the 30 movie-shaped ones (<= 12 video files) are the sample.
`design/MULTIFILE-RELEASES.md` records the measurement and the two false
positives that shaped the extras test -- read it before loosening anything.
"""

import re

# No release-group names here. `rarbg` used to be in this list and matched
# `...AAC-RARBG.mp4`, which IS the feature; the 10%-of-largest rule below
# catches the group's actual junk file without the risk.
_EXTRA_WORDS = (
    "bts",
    "behind the scenes",
    "behind-the-scenes",
    "trailer",
    "teaser",
    "sample",
    "preview",
    "outtake",
    "blooper",
    "photoshoot",
    "promo",
)

# A named extra still has to be small. `Pirates 2005 and Pirates 2 2008 Bonus
# Edition` put `bonus` in the name of both halves of a double feature, and
# without a size test both features would have sorted to the end.
_NAMED_EXTRA_MAX_SHARE = 0.5

# Unnamed junk: the 0.00 GB `RARBG.com.mp4` of the world.
_ANY_EXTRA_MAX_SHARE = 0.1

_PART_MARKER = re.compile(r"(?:part|pt|cd|disc|disk)[\s._-]*(\d+)", re.IGNORECASE)
_DIGIT_RUN = re.compile(r"\d+")


def _name(entry) -> str:
    return (getattr(entry, "original_filename", None) or "").lower()


def _size(entry) -> int:
    return getattr(entry, "file_size", None) or 0


def is_extra(entry, largest: int) -> bool:
    """Whether this file is bonus material rather than part of the title."""

    size = _size(entry)

    if largest and size < largest * _ANY_EXTRA_MAX_SHARE:
        return True

    if not any(word in _name(entry) for word in _EXTRA_WORDS):
        return False

    # Unknown sizes must not make a named file an extra on the strength of the
    # name alone -- that is the double-feature case.
    return bool(largest) and size < largest * _NAMED_EXTRA_MAX_SHARE


def _by_shared_digit_run(entries) -> list | None:
    """Order by a digit run that varies while everything before it matches.

    The workhorse: 19 of the 30 measured torrents. Requiring the text BEFORE
    the run to be identical across every file is what stops a resolution or a
    year being read as a scene index.
    """

    if len(entries) < 2:
        return None

    runs = []

    for entry in entries:
        name = _name(entry)
        runs.append(
            {name[: match.start()]: int(match.group()) for match in _DIGIT_RUN.finditer(name)}
        )

    for prefix in runs[0]:
        if not all(prefix in run for run in runs):
            continue

        values = [run[prefix] for run in runs]

        if len(set(values)) != len(values):
            continue

        return [entry for _, entry in sorted(zip(values, entries), key=lambda pair: pair[0])]

    return None


def _by_part_marker(entries) -> list | None:
    """Order by an explicit `part`/`cd`/`disc` number.

    Nothing in the sample needed this -- `divxfactory-fsd2_part1` is caught by
    the digit run above. It is kept because one rename ("fsd2 part 1") breaks
    that shared prefix and lands here instead.
    """

    if len(entries) < 2:
        return None

    numbers = []

    for entry in entries:
        found = _PART_MARKER.findall(_name(entry))

        if len(found) != 1:
            return None

        numbers.append(int(found[0]))

    if len(set(numbers)) != len(numbers):
        return None

    return [entry for _, entry in sorted(zip(numbers, entries), key=lambda pair: pair[0])]


def _ordered(entries: list) -> list:
    """One group of files, by the first signal that gives a total order."""

    return (
        _by_shared_digit_run(entries)
        or _by_part_marker(entries)
        # Not a fallback so much as the right answer for performer-named
        # scenes ("Ava Sinclaire.mp4"), which have no intrinsic order: 11 of
        # the 30. It is also what this code did before ordering existed.
        or sorted(entries, key=_name)
    )


def order_parts(entries: list) -> list:
    """The files of one release, feature first and bonus material last."""

    if len(entries) < 2:
        return list(entries)

    largest = max(_size(entry) for entry in entries)
    extras = [entry for entry in entries if is_extra(entry, largest)]

    # If everything looks like an extra, nothing is -- otherwise a release
    # whose files all carry the same word (or all report no size) would be
    # reordered on no information at all.
    if len(extras) == len(entries):
        extras = []

    main = [entry for entry in entries if entry not in extras]

    return _ordered(main) + _ordered(extras)
