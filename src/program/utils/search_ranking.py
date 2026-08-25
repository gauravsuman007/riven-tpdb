"""Ordering full-text search results by how well the title matches.

TPDB's ``q`` search returns matches in no useful order and silently ignores
every ordering parameter it accepts, so the closest title is routinely not on
the first page. Searching "pirates" put Digital Playground's Pirates -- the
best-selling adult film ever made, and an exact title match -- on page 2,
behind an unrelated 2018 title of the same name and eleven Butthole Pirates
sequels. TPDB's page size is fixed at 20 whatever ``per_page`` says, so a UI
showing one page never saw it.

Kept separate from the router so it can be tested without standing up FastAPI.
"""

from program.services.awards.matching import title_ratio


def relevance(query: str, title: str | None) -> tuple[int, float]:
    """How well one result title answers the query. Higher sorts first.

    Tiered, and the tiers do most of the work: "Pirates" and
    "Butthole Pirates #4" both contain every token of the query, so token
    overlap alone cannot separate them. Exact match wins, then a title that
    starts with the query, then one that merely contains it, and the overlap
    ratio only breaks ties inside a tier.
    """

    name = (title or "").strip().lower()
    wanted = query.strip().lower()

    if not name or not wanted:
        return (0, 0.0)

    if name == wanted:
        tier = 3
    elif name.startswith(wanted):
        tier = 2
    elif wanted in name:
        tier = 1
    else:
        tier = 0

    return (tier, title_ratio(wanted, name))


def rank(query: str, records: list) -> list:
    """Sort records by title relevance, most relevant first.

    Stable, so records of equal relevance keep the order the source gave them.
    """

    return sorted(records, key=lambda r: relevance(query, getattr(r, "title", None)), reverse=True)
