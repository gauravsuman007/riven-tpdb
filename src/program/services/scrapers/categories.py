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
    """Pick the category ids relevant to an item.

    Args:
        item_type: The item's type ("movie", "show", ...).
        is_anime: Whether the item is anime.
        is_adult: Whether the item is adult (has a TPDB id).
        categories: Indexer categories as ``(type, ids)`` tuples.
    """

    ids: set[int] = set()

    for category_type, category_ids in categories:
        if (
            category_type == item_type
            or (category_type == "anime" and is_anime)
            or (category_type == "xxx" and is_adult)
        ):
            ids.update(category_ids)

    return ids