"""AVN Award corpus, sourced from Wikipedia's per-ceremony articles.

Why Wikipedia and not AVN itself: ``awards.avn.com`` only publishes 2019 onward
and its year switcher is client-side, so each year needs a browser. The
Wikipedia articles cover the 4th ceremony (1987) through the current one,
include nominees as well as winners, and are reachable through the MediaWiki
API. Wikidata was also evaluated and rejected -- its AVN statements are almost
entirely performers (221 humans against 21 films), so it carries no titles.

Two article layouts exist and both are handled:

    * 39th onward: the category is inline in the cell as
      ``{{Award category|#89cff0|Name}}``.
    * Older: a row of ``!`` header cells names the categories and the entries
      sit in the *following* row, so a cell's category is positional. This is
      why :mod:`.wikitable` exists -- a flat regex mis-attributes these.

Within a cell, ``* '''Entry'''`` is the winner and ``** Entry`` lines are the
other nominees.

A third layout carries most of the winners and sits *outside* any table: the
"Additional award winners" sections list one line per category as
``* '''Category:''' ''Title''``. Ignoring these loses 2,595 winners across 33
ceremonies -- more than the tables hold.
"""

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from loguru import logger

from program.services.awards.wikitable import iter_tables

WIKI_API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "Riven-TPDB/1.0 (https://github.com/rivenmedia/riven)"

# The 1st-3rd and 5th ceremonies are redirects with no content; the range is
# open-ended at the top so a new ceremony is picked up without a code change.
FIRST_CEREMONY = 4

# Ceremony N honours work from year N + 1982 and is held in N + 1983.
CEREMONY_YEAR_OFFSET = 1983

# Categories that award a business, product or website rather than a film.
# Small (about 23 entries) but they would otherwise enter the library as
# titles named "Brazzers" or "Best Retail Chain - Large".
NON_MEDIA_CATEGORY = re.compile(
    r"(?i)\b(?:web\s?site|website|web retail|retail chain|pleasure product|"
    r"manufacturer|distributor|marketing|packaging|store|novelty|toy|"
    r"social media|cam\s?girl|cam/creator|web starlet|crossover star|"
    r"porn star website|company image)\b"
)

# Categories whose award goes to a person. Modern articles quote the work
# ("Best Actor - Featurette"), and the quoted form always wins; older ones write
# it bare as "Person, Title", which is only safe to split on a comma once the
# category is known to be a person award.
PERSON_CATEGORY = re.compile(
    r"(?i)(?:performer of the year|starlet(?: of the year)?|"
    r"director of the year|executive of the year|"
    r"performer of the decade|hall of fame|"
    r"\bact(?:or|ress)\b|\bdirector\b|supporting|"
    r"cinematograph|editing|\bmusic\b|screenplay|writer|art direction|"
    r"make ?up|special effects|tease performance|non-?sex performance)"
)

_REF = re.compile(r"<ref[^>]*/>|<ref.*?</ref>", re.S)
_DAGGER = re.compile(r"\{\{(?:double-)?dagger\}\}", re.I)
_LINK = re.compile(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]")
_TEMPLATE = re.compile(r"\{\{[^{}]*\}\}")
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_CAT_INLINE = re.compile(r"\{\{Award category\|[^|]*\|([^}]+)\}\}")
_BULLET = re.compile(r"^(\*+)\s*(.+)$")

# ``* '''Category:''' Entry`` from the "Additional award winners" lists. The
# colon sits inside the bold in some years and outside it in others.
_INLINE_CATEGORY = re.compile(r"^\*\s*'''(?P<cat>[^']{3,80}?):?'''\s*:?\s*(?P<body>.+)$")

_TABLE_OPEN = re.compile(r"^\{\|")
_TABLE_CLOSE = re.compile(r"^\|\}")

# A quoted work title. Straight and curly quotes both occur, sometimes mixed
# within one article.
_QUOTED = re.compile(r"[\"“„]([^\"“”„]+)[\"”]")


def _pre_markup(text: str) -> str:
    """Strip refs, comments and templates but keep ``''italic''`` markers.

    Entry parsing needs the italic markers to find the trailing studio, so this
    stops short of :func:`_strip_markup`. Refs must go first: a citation like
    ``<ref name="AVN-mag" />`` otherwise looks exactly like a quoted work title
    and is picked up as one.
    """

    text = _COMMENT.sub("", text)
    text = _REF.sub("", text)
    text = _DAGGER.sub("", text)
    text = _LINK.sub(r"\1", text)

    # Templates can nest one level in these articles; two passes is enough.
    text = _TEMPLATE.sub("", text)
    text = _TEMPLATE.sub("", text)

    return re.sub(r"\s+", " ", text).strip()


def _strip_markup(text: str) -> str:
    """Reduce wikitext to plain text, keeping link labels."""

    return re.sub(r"'{2,}", "", _pre_markup(text)).strip()


def _is_bold(raw: str) -> bool:
    return raw.lstrip().startswith("'''")


def _clean(text: str) -> str:
    return _strip_markup(text).strip(" –-,;|")


@dataclass(slots=True)
class AwardEntry:
    """One nomination or win from a ceremony."""

    ceremony: int
    year: int
    category: str
    winner: bool
    raw: str
    title: str | None = None
    studio: str | None = None
    performers: list[str] = field(default_factory=list)

    @property
    def is_media(self) -> bool:
        """Whether this entry names a work that could exist in a library."""

        return bool(self.title)


def ordinal(n: int) -> str:
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def ceremony_year(ceremony: int) -> int:
    """The calendar year the ceremony was held (the year AVN brands it with)."""

    return ceremony + CEREMONY_YEAR_OFFSET


def article_title(ceremony: int) -> str:
    return f"{ordinal(ceremony)} AVN Awards"


def fetch_articles(ceremonies: list[int]) -> dict[int, str | None]:
    """Fetch ceremony wikitext, up to 20 articles per request.

    Returns ``None`` for a ceremony whose article is missing or is a redirect
    stub, so callers can tell "no data" from "no entries".
    """

    by_title = {article_title(c): c for c in ceremonies}
    out: dict[int, str | None] = {c: None for c in ceremonies}
    titles = list(by_title)

    for start in range(0, len(titles), 20):
        batch = titles[start : start + 20]
        query = urllib.parse.urlencode(
            {
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "prop": "revisions",
                "rvprop": "content",
                "rvslots": "main",
                "titles": "|".join(batch),
            }
        )
        request = urllib.request.Request(
            f"{WIKI_API}?{query}", headers={"User-Agent": USER_AGENT}
        )

        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)

        for page in payload.get("query", {}).get("pages", []):
            ceremony = by_title.get(page.get("title", ""))

            if ceremony is None or page.get("missing"):
                continue

            revisions = page.get("revisions") or []

            if not revisions:
                continue

            content = revisions[0]["slots"]["main"]["content"]

            # Redirect stubs are ~150 characters and carry no award tables.
            out[ceremony] = content if len(content) > 1000 else None

    return out


def _split_entry(raw: str, category: str) -> tuple[str | None, str | None, list[str]]:
    """Pull (title, studio, performers) out of one entry line.

    Layouts seen across the corpus::

        Strip - ''Dorcel/Pulse''                      -> title + studio
        Tommy Pistol, "Mr. Sicko..." - ''Kink''       -> performer + title + studio
        '''''Suicide Squad XXX'''''                   -> title (bold italic)
        Adriana Chechik                               -> performer only

    The studio is the trailing italic segment after the final en dash. A quoted
    segment always wins as the title, because in performer categories the work
    is what is quoted.
    """

    text = _pre_markup(raw)
    studio = None

    # Trailing " - ''Studio''" (en dash in modern articles, hyphen in some old).
    studio_match = re.search(r"[–—-]\s*''([^']+)''\s*$", text)

    if studio_match:
        studio = _clean(studio_match.group(1))
        text = text[: studio_match.start()]

    # Scene categories append the cast after the studio as
    # "Studio; Performer, Performer". Keep the studio, promote the rest.
    trailing_cast: list[str] = []

    if studio and ";" in studio:
        studio, _, cast = studio.partition(";")
        studio = studio.strip()
        trailing_cast = [c for c in (_clean(x) for x in cast.split(",")) if c]

    quoted = _QUOTED.search(text)

    if quoted:
        title = _clean(quoted.group(1))
        before = _clean(text[: quoted.start()])
        performers = [p for p in (_clean(x) for x in re.split(r"\s*&\s*|,", before)) if p]

        return title or None, studio, performers + trailing_cast

    plain = _clean(text)

    if not plain:
        return None, studio, []

    # "Performer, Title" in older person categories where nothing is quoted:
    # only trust this when the category is a person award, otherwise a title
    # containing a comma would be split.
    if PERSON_CATEGORY.search(category):
        parts = [p.strip() for p in plain.split(",", 1)]

        if len(parts) == 2 and parts[1]:
            return _clean(parts[1]) or None, studio, [parts[0]] + trailing_cast

        return None, studio, [plain] + trailing_cast

    return plain, studio, trailing_cast


def _parse_inline_lists(ceremony: int, wikitext: str) -> list[AwardEntry]:
    """Extract ``* '''Category:''' Entry`` winners from outside the tables.

    These sections list winners only -- there are no nominees to pair them
    with -- so every match is recorded as a win.
    """

    year = ceremony_year(ceremony)
    entries: list[AwardEntry] = []
    depth = 0

    for line in wikitext.splitlines():
        stripped = line.strip()

        if _TABLE_OPEN.match(stripped):
            depth += 1
            continue

        if _TABLE_CLOSE.match(stripped):
            depth = max(0, depth - 1)
            continue

        if depth:
            continue

        match = _INLINE_CATEGORY.match(stripped)

        if not match:
            continue

        category = _clean(match.group("cat"))

        if not category or NON_MEDIA_CATEGORY.search(category):
            continue

        title, studio, performers = _split_entry(match.group("body"), category)

        entries.append(
            AwardEntry(
                ceremony=ceremony,
                year=year,
                category=category,
                winner=True,
                raw=_clean(match.group("body")),
                title=title,
                studio=studio,
                performers=performers,
            )
        )

    return entries


def parse_ceremony(ceremony: int, wikitext: str) -> list[AwardEntry]:
    """Extract every winner and nominee from one ceremony article."""

    year = ceremony_year(ceremony)
    entries: list[AwardEntry] = []

    for rows in iter_tables(wikitext):
        headers: list[str] = []

        for row in rows:
            if row and all(cell.header for cell in row):
                headers = [_clean(cell.text) for cell in row]
                continue

            for index, cell in enumerate(row):
                if cell.header:
                    continue

                inline = _CAT_INLINE.search(cell.text)

                if inline:
                    category = _clean(inline.group(1))
                elif index < len(headers):
                    category = headers[index]
                else:
                    category = ""

                if not category or NON_MEDIA_CATEGORY.search(category):
                    continue

                for line in cell.text.splitlines():
                    bullet = _BULLET.match(line.strip())

                    if not bullet:
                        continue

                    marker, body = bullet.groups()
                    title, studio, performers = _split_entry(body, category)

                    entries.append(
                        AwardEntry(
                            ceremony=ceremony,
                            year=year,
                            category=category,
                            winner=len(marker) == 1 and _is_bold(body),
                            raw=_clean(body),
                            title=title,
                            studio=studio,
                            performers=performers,
                        )
                    )

    entries.extend(_parse_inline_lists(ceremony, wikitext))

    return _dedupe(entries)


def _dedupe(entries: list[AwardEntry]) -> list[AwardEntry]:
    """Drop entries that appear in both a table and an "additional winners" list.

    A few ceremonies repeat a category in both layouts; without this the same
    title would be requested twice and counted twice in any award tally.
    """

    seen: set[tuple[str, str, bool]] = set()
    out: list[AwardEntry] = []

    for entry in entries:
        key = (entry.category.casefold(), (entry.title or entry.raw).casefold(), entry.winner)

        if key in seen:
            continue

        seen.add(key)
        out.append(entry)

    return out


def build_corpus(ceremonies: list[int] | None = None) -> list[AwardEntry]:
    """Fetch and parse every requested ceremony.

    A ceremony that fails to fetch or parse is logged and skipped rather than
    failing the whole corpus, so one bad article cannot block a refresh.
    """

    if ceremonies is None:
        ceremonies = list(range(FIRST_CEREMONY, _latest_ceremony() + 1))

    articles = fetch_articles(ceremonies)
    corpus: list[AwardEntry] = []

    for ceremony in ceremonies:
        wikitext = articles.get(ceremony)

        if not wikitext:
            logger.debug(f"AVN ceremony {ceremony} has no usable article")
            continue

        try:
            parsed = parse_ceremony(ceremony, wikitext)
        except Exception as exc:
            logger.error(f"Failed to parse AVN ceremony {ceremony}: {exc}")
            continue

        corpus.extend(parsed)

    return corpus


def _latest_ceremony() -> int:
    """Highest ceremony number with an article, probed upward from a known one.

    Hardcoding the top of the range would silently freeze the corpus the year a
    new ceremony is added.
    """

    known = 43
    probe = known

    while probe < known + 5:
        articles = fetch_articles([probe + 1])

        if not articles.get(probe + 1):
            break

        probe += 1

    return probe
