"""Torrent-indexer category helpers (Prowlarr/Jackett).

Adult content lives under Newznab category 6000 ("XXX"/"Adult"). Riven's
default category mapping only recognizes TV/Movies/Anime, which silently drops
adult indexers. These helpers make adult categories first-class so TPDB items
can be searched on adult trackers.
"""

ADULT_CATEGORY_HINTS = ("xxx", "adult", "porn", "18+")


def is_adult_category(name: str | None) -> bool:
    """Return True if a category name denotes adult (XXX) content."""

    if not name:
        return False

    lowered = name.lower()
    return any(hint in lowered for hint in ADULT_CATEGORY_HINTS)


def select_category_ids(
    item_type: str,
    is_anime: bool,
    is_adult: bool,
    categories: list[tuple[str, list[int]]],
) -> set[int]:
    """Pick the category ids relevant to an item, for one indexer.

    An adult title searches the indexer's XXX categories *instead of* its
    Movies categories, not in addition to them. Searching both is not merely
    wasteful -- it actively buries the results you want. A manual scrape for
    "Pirates" (Digital Playground, 2005) returned 192 releases of which every
    single survivor was mainstream: five Pirates of the Caribbean films, Pirates
    of Silicon Valley, and a dozen unrelated films from a release group called
    PiRaTeS. A one-word adult title collides with mainstream cinema constantly,
    and the mainstream categories are far larger, so the real matches lose.

    The fallback matters though: an adult-only tracker whose categories
    Prowlarr maps to "movie" exposes no XXX category at all, and restricting it
    to a category set it does not have would search nothing. So the swap only
    happens for indexers that actually offer XXX.

    Args:
        item_type: The item's type ("movie", "show", ...).
        is_anime: Whether the item is anime.
        is_adult: Whether the item is adult (has a TPDB or Adult Empire id).
        categories: This indexer's categories as ``(type, ids)`` tuples.
    """

    if is_adult:
        adult_ids = {
            category_id
            for category_type, category_ids in categories
            if category_type == "xxx"
            for category_id in category_ids
        }

        if adult_ids:
            return adult_ids

    ids: set[int] = set()

    for category_type, category_ids in categories:
        if (
            category_type == item_type
            or (category_type == "anime" and is_anime)
            or (category_type == "xxx" and is_adult)
        ):
            ids.update(category_ids)

    return ids
