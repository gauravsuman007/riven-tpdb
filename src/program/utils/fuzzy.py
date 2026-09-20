"""Name matching that survives a typo, shared by every "find me a X" search.

WHY THIS EXISTS

`ilike('%term%')` is not search, it is a substring test, and the difference
shows up the moment somebody types the name the way they say it rather than
the way it is spelled. Measured against the live directory before this module
existed:

    "brazzers"   -> Brazzers                       (found)
    "brazers"    -> nothing                        (a single dropped letter)
    "evil angel" -> Evil Angel, and 21 others      (found)
    "evilangel"  -> nothing                        (a missing space)

Both misses are the same class of failure, and a viewer who gets an empty
page concludes the studio is not carried rather than that they mistyped it.

WHAT IT DOES

Two orthogonal pieces, because filtering and ordering want different things:

    `matches()`  widens the WHERE clause -- substring OR collapsed-substring
                 OR trigram-similar.
    `ranking()`  orders what came back, exact before prefix before substring
                 before merely-similar, so widening never costs precision at
                 the top of the list. A studio named exactly what was typed
                 is still first even when forty others are 0.4 similar.

COLLAPSING is the cheap half and catches more than trigrams do: stripping
everything but letters and digits makes "evilangel", "Evil-Angel" and
"evil angel" one string. It is exact, so it cannot introduce a wrong match.

TRIGRAMS are the expensive half and need `pg_trgm`. Postgres only: on SQLite
(which is what the test harness runs) those clauses are simply omitted, so
search stays CORRECT everywhere and only gets cleverer where the extension
exists. Never make behaviour depend on an index -- see `AGENTS.md` on the
library's trigram indexes, where exactly that was the rule.

`word_similarity`, NOT `similarity`. The plain form compares whole strings
and so punishes a name for being longer than the query, which is the normal
case here: measured, "bangbross" against "Bang Bros Productions" scores 0.24
whole-string and 0.50 word-wise, and "vixn" against "Vixen" 0.375 against
0.60. A viewer types the bit they remember, not the full registered name.
"""

from __future__ import annotations

import re

from sqlalchemy import ColumnElement, String, case, cast, func, literal, or_

#: Below this, a word-trigram match is noise. Measured against the live
#: directory: "brazers" -> Brazzers scores 0.70, "vixn" -> Vixen 0.60,
#: "bangbross" -> Bang Bros Productions exactly 0.50. Two matches each at this
#: threshold; lowering it to pg_trgm's own 0.3 default starts returning names
#: that merely share a syllable.
SIMILARITY = 0.5

#: A term shorter than this is a prefix someone is still typing, not a
#: misspelling. Fuzzy matching on one or two letters matches nearly anything.
MIN_FUZZY = 4

_NOISE = re.compile(r"[^a-z0-9]+")


def collapse(value: str) -> str:
    """Lowercase, and drop everything that is not a letter or a digit."""

    return _NOISE.sub("", (value or "").lower())


def has_trigrams(session) -> bool:
    """Is this the database that can do similarity, or the test one?"""

    try:
        return session.bind.dialect.name == "postgresql"
    except AttributeError:
        return False


def matches(
    term: str,
    *columns: ColumnElement[str],
    session=None,
    collapsed: ColumnElement[str] | None = None,
) -> ColumnElement[bool]:
    """A WHERE clause for "one of these columns is roughly this".

    `collapsed` is for tables that already store a punctuation-free form of
    the name -- the OnlyFans index stores one, because a handle IS the
    collapsed spelling -- and saves collapsing the column in SQL.
    """

    term = (term or "").strip()

    if not term:
        return literal(True)

    clauses: list[ColumnElement[bool]] = [
        column.ilike(f"%{term}%") for column in columns
    ]

    flat = collapse(term)

    if collapsed is not None and flat:
        clauses.append(collapsed.like(f"%{flat}%"))

    if flat and len(flat) >= MIN_FUZZY and has_trigrams(session):
        clauses.extend(
            func.word_similarity(term.lower(), func.lower(column))
            >= SIMILARITY
            for column in columns
        )

    return or_(*clauses)


def ranking(
    term: str,
    column: ColumnElement[str],
    *,
    session=None,
    collapsed: ColumnElement[str] | None = None,
) -> list[ColumnElement]:
    """ORDER BY terms putting the best match first. Use before any tiebreak.

    Returned as a list so the caller can append its own ordering after it --
    the studio directory falls back to catalogue size, the account index to
    whatever the rail asked for.
    """

    term = (term or "").strip()

    if not term:
        return []

    lowered = func.lower(column)
    low = term.lower()
    flat = collapse(term)

    # Lower sorts first, so these read as "0 is the best answer there is".
    # Built as a list rather than inline so the optional collapsed rungs do
    # not turn the ladder into a conditional expression nobody can read.
    rungs: list[tuple[ColumnElement[bool], int]] = [(lowered == low, 0)]

    if collapsed is not None and flat:
        rungs.append((collapsed == flat, 0))

    rungs.append((lowered.like(f"{low}%"), 1))

    if collapsed is not None and flat:
        rungs.append((collapsed.like(f"{flat}%"), 1))

    rungs.append((lowered.like(f"%{low}%"), 2))

    tier = case(*rungs, else_=3)

    order: list[ColumnElement] = [tier]

    if flat and len(flat) >= MIN_FUZZY and has_trigrams(session):
        order.append(func.word_similarity(low, lowered).desc())

    # Shorter names first within a tier: searching "vixen" should offer
    # "Vixen" above "Exotic Vixen Films", and both are tier 2.
    order.append(func.length(cast(column, String)))

    return order
