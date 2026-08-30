"""AVN Award winners, sourced from ``awards.avn.com`` itself.

WHY A SECOND SOURCE. The Wikipedia corpus (:mod:`.avn`) carries most ceremonies
well, but three do not have the "Additional award winners" section that holds
the bulk of the winners -- and that section, not the tables, is where the films
are. Measured against the live articles:

    ceremony   media entries   winners
    43rd/2026        416            41
    42nd/2025         10             1
    38th/2021         20             2
    37th/2020         21             3
    25th/2008        166            63

So 2020, 2021 and 2025 landed in the library with one to three titles each,
and it reads as a broken importer. It is not: the data is simply absent
upstream, and no amount of parsing recovers it.

``awards.avn.com/winners/{year}`` publishes those same ceremonies -- 2019
onward, which covers every thin year -- as ordinary server-rendered HTML.
The module docstring in :mod:`.avn` records this source as rejected because
"its year switcher is client-side, so each year needs a browser"; that is true
of the switcher, and irrelevant, because each year also has its own plain URL.

The markup carries explicit hooks::

    <h3 data-awards-result-category="">Best Art Direction</h3>
    <p data-awards-result-entry=""><strong>Project X</strong>, Digital Playground</p>

Those two attributes are the anchors. They are semantic rather than
presentational, so they survive the styling churn that would break a
class-based selector -- but if AVN ever drops them this yields nothing, which
is why :func:`winners_for_year` returning empty is logged rather than being
allowed to quietly wipe a year.

This source lists WINNERS ONLY. There are no nominees to pair them with, so
every entry is recorded as a win, exactly as the inline-list path in
:mod:`.avn` does.
"""

from __future__ import annotations

import html
import re
import urllib.error
import urllib.request

from loguru import logger

from program.services.awards.avn import (
    CEREMONY_YEAR_OFFSET,
    USER_AGENT,
    AwardEntry,
)

BASE_URL = "https://awards.avn.com/winners"

#: The first ceremony this site publishes. Below it there is nothing to fetch
#: and Wikipedia is the only source.
FIRST_YEAR = 2019

_CATEGORY = re.compile(
    r"<h3[^>]*\bdata-awards-result-category\b[^>]*>(.*?)</h3>", re.I | re.S
)
_ENTRY = re.compile(
    r"<p[^>]*\bdata-awards-result-entry\b[^>]*>(.*?)</p>", re.I | re.S
)
#: One category heading followed by its entry paragraph. Matched as a pair
#: rather than separately, because a category with no entry must not silently
#: absorb the next category's winner.
_PAIR = re.compile(
    _CATEGORY.pattern + r"\s*" + _ENTRY.pattern, re.I | re.S
)

_TAG = re.compile(r"<[^>]+>")
#: The site's editors paste from a word processor, so bold survives as literal
#: asterisks in about a tenth of entries ("**He'**<strong>s in Charge 4</strong>").
#: Stripped rather than interpreted -- they mark emphasis, never structure.
_STRAY_EMPHASIS = re.compile(r"\*+")
_QUOTED = re.compile(r"[\"“‘']([^\"”’']+)[\"”’']")


def _text(fragment: str) -> str:
    """HTML fragment to plain text, normalised for the splitter below."""

    plain = html.unescape(_TAG.sub(" ", fragment))
    plain = _STRAY_EMPHASIS.sub("", plain)
    # Non-breaking spaces are common here and would defeat every strip().
    plain = plain.replace("\xa0", " ")

    return re.sub(r"\s+", " ", plain).strip(" .,;|")


def split_entry(raw: str) -> tuple[str | None, str | None, list[str]]:
    """Pull (title, studio, performers) out of one winner line.

    The three shapes this source uses::

        Project X, Digital Playground
        "Nine" | Kink Label 3, Deeper/Pulse; Angel Windell & Chris Diamond
        Kayden Kross

    A quoted segment wins as the title, matching :mod:`.avn`'s convention: in
    a scene category the scene is what is quoted, and it is the scene that
    exists as a row on TPDB.
    """

    text = _text(raw)

    if not text:
        return None, None, []

    performers: list[str] = []
    quoted = _QUOTED.search(text)
    title: str | None = None

    if quoted:
        title = _text(quoted.group(1)) or None
        # Everything after the scene name describes the release it came from.
        remainder = text[quoted.end() :].lstrip(" |")
    else:
        remainder = text

    # "Studio; Performer & Performer" -- the cast is appended after a semicolon
    # in every scene category and never appears without one.
    if ";" in remainder:
        remainder, _, cast = remainder.partition(";")
        performers = [
            p for p in (_text(x) for x in re.split(r"\s*&\s*|,", cast)) if p
        ]

    remainder = remainder.strip(" ,")

    if title is None:
        # "Title, Studio". Split on the LAST comma: studios are single tokens
        # ("Digital Playground", "Vixen/Pulse") while titles routinely contain
        # commas.
        head, sep, tail = remainder.rpartition(",")

        if not sep or not head.strip():
            return _text(remainder) or None, None, performers

        studio = _text(tail) or None

        """
        Pre-2021 ceremonies put the cast first and quote nothing:

            Riley Reid, Angela White & Katrina Jade, I Am Riley, Evil Angel

        Without this the whole cast list becomes the title. Gated on an "&"
        AND a further comma, which together only occur when a list of names
        precedes the work -- a plain "Sex & the City, Wicked" has the
        ampersand but no second comma, and is left alone.
        """
        if "&" in head and "," in head:
            names, _, work = head.rpartition(",")

            if work.strip():
                performers = [
                    p for p in (_text(x) for x in re.split(r"\s*&\s*|,", names)) if p
                ] + performers

                return _text(work) or None, studio, performers

        return _text(head) or None, studio, performers

    return title, _text(remainder) or None, performers


def fetch_year(year: int) -> str | None:
    """The winners page for one ceremony year, or None when unavailable."""

    request = urllib.request.Request(
        f"{BASE_URL}/{year}", headers={"User-Agent": USER_AGENT}
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        # 404 is the ordinary answer for a year this site does not publish.
        logger.debug(f"AVN site returned {exc.code} for {year}")
    except Exception as exc:
        logger.warning(f"Could not fetch the AVN winners page for {year}: {exc}")

    return None


def winners_for_year(year: int) -> list[AwardEntry]:
    """Every winner this site lists for `year`, as award entries.

    Category filtering is left to the caller: :attr:`AwardEntry.is_media`
    applies the same gates the Wikipedia path uses, so both sources are
    admitted on identical terms.
    """

    page = fetch_year(year)

    if not page:
        return []

    ceremony = year - CEREMONY_YEAR_OFFSET
    entries: list[AwardEntry] = []

    for category_html, entry_html in _PAIR.findall(page):
        category = _text(category_html)
        raw = _text(entry_html)

        if not category or not raw:
            continue

        title, studio, performers = split_entry(entry_html)

        entries.append(
            AwardEntry(
                ceremony=ceremony,
                year=year,
                category=category,
                winner=True,
                raw=raw,
                title=title,
                studio=studio,
                performers=performers,
            )
        )

    if not entries:
        # Loud, because the alternative is a year silently reverting to the
        # handful Wikipedia holds with nothing to say why.
        logger.warning(
            f"AVN winners page for {year} parsed to nothing -- "
            "the data-awards-result-* hooks have probably changed"
        )

    return entries
