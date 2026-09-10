"""Pure mapping helpers from StashDB GraphQL JSON to Riven movie dicts.

Deliberately the same output shape as `tpdb_mapping.scene_to_movie_dict`, key
for key, so a `Movie` built from StashDB is indistinguishable downstream from
one built from TPDB. Everything after the indexer -- ranking, matching, the
API, the frontend -- reads those keys and must not need to know which
provider supplied them.

Stdlib only, like its TPDB counterpart, so the mapping can be tested without
standing anything up.
"""

from datetime import datetime
from typing import Any


def parse_stashdb_date(value: Any) -> datetime | None:
    """Parse a StashDB `release_date` into a datetime, or None.

    StashDB dates are plain `YYYY-MM-DD` strings, but partial dates ("2007",
    "2007-09") do appear on older records. A partial date is better than none
    -- the year is what date matching mostly uses -- so it is completed to the
    first of the month rather than discarded.
    """

    if value is None:
        return None

    if isinstance(value, datetime):
        return value

    text = str(value).strip()

    if not text:
        return None

    for fmt, padded in (("%Y-%m-%d", text), ("%Y-%m", f"{text}-01"), ("%Y", f"{text}-01-01")):
        try:
            return datetime.strptime(padded, "%Y-%m-%d")
        except ValueError:
            continue

    return None


def _poster(scene: dict[str, Any]) -> str | None:
    """The largest image StashDB has for this scene.

    There is no "primary" flag in the schema and the list comes back
    unordered, so the biggest by pixel count is the best available proxy for
    "the cover". Images missing their dimensions sort last rather than
    crashing the comparison.
    """

    images = [
        image
        for image in (scene.get("images") or [])
        if isinstance(image, dict) and image.get("url")
    ]

    if not images:
        return None

    def area(image: dict[str, Any]) -> int:
        try:
            return int(image.get("width") or 0) * int(image.get("height") or 0)
        except (TypeError, ValueError):
            return 0

    return max(images, key=area)["url"]


def _performer_names(scene: dict[str, Any]) -> list[str] | None:
    """Performer names, preferring the credited alias.

    `as` is the name the scene credits, which is the one that appears in
    release titles -- and release titles are what these names are matched
    against. The canonical `performer.name` is the fallback.
    """

    names = list[str]()

    for appearance in scene.get("performers") or []:
        if not isinstance(appearance, dict):
            continue

        performer = appearance.get("performer")
        name = appearance.get("as") or (
            performer.get("name") if isinstance(performer, dict) else None
        )

        if name:
            names.append(str(name))

    return names or None


def _tag_names(scene: dict[str, Any]) -> list[str] | None:
    names = [
        str(tag["name"]).lower()
        for tag in (scene.get("tags") or [])
        if isinstance(tag, dict) and tag.get("name")
    ]

    return names or None


def _studio(scene: dict[str, Any]) -> tuple[str | None, str | None]:
    """The studio id and name, as `site_id`/`site_name`.

    StashDB nests a `parent` studio -- "Digital Playground" as the parent of
    a particular series or site. The IMMEDIATE studio is used, matching what
    TPDB calls the site, because that is the name release titles carry.
    """

    studio = scene.get("studio")

    if not isinstance(studio, dict):
        return None, None

    studio_id = studio.get("id")

    return (str(studio_id) if studio_id else None), studio.get("name")


def scene_to_movie_dict(scene: dict[str, Any]) -> dict[str, Any]:
    """Map a StashDB scene onto the same dict a TPDB scene maps to."""

    site_id, site_name = _studio(scene)
    aired_at = parse_stashdb_date(scene.get("release_date"))

    return {
        "title": scene.get("title") or "Untitled",
        "poster_path": _poster(scene),
        "year": aired_at.year if aired_at else None,
        # Deliberately NOT populated: a StashDB UUID is not a TPDB id, and
        # putting one in that column would make every `tpdb_id` lookup, dedupe
        # and "already in the library" check silently wrong.
        "tpdb_id": None,
        "stashdb_id": scene.get("id"),
        "site_id": site_id,
        "site_name": site_name,
        "performers": _performer_names(scene),
        "genres": _tag_names(scene),
        "aired_at": aired_at,
        # StashDB has no ratings. None, not 0: a zero would sort as "rated
        # badly" wherever a rating is compared.
        "rating": None,
        "content_rating": None,
        "type": "movie",
    }
