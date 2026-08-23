"""Pure mapping helpers from ThePornDB JSON to Riven movie dicts.

Kept free of framework imports (stdlib only) so the mapping can be unit
tested in isolation and reused by any consumer of TPDB data. The JSON
shapes match ThePornDB's REST API contract (see the official
ThePornDatabase/Jellyfin.Plugin.ThePornDB models).
"""

from datetime import datetime
from typing import Any


def parse_tpdb_date(value: Any) -> datetime | None:
    """Parse a TPDB date value into a datetime, or None if absent/invalid."""

    if value is None:
        return None

    if isinstance(value, datetime):
        return value

    text = str(value).strip()

    if not text:
        return None

    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19] if len(text) > 19 else text, fmt)
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _poster(scene: dict[str, Any]) -> str | None:
    """Pick the best available poster URL (prefer high-res, then the flat one)."""

    posters = scene.get("posters")

    if isinstance(posters, dict):
        for size in ("large", "full", "medium", "small"):
            if posters.get(size):
                return posters[size]

    return scene.get("poster")


def _performer_names(scene: dict[str, Any]) -> list[str] | None:
    performers = scene.get("performers") or []

    names = [
        performer.get("name")
        for performer in performers
        if isinstance(performer, dict) and performer.get("name")
    ]

    return names or None


def _tag_names(scene: dict[str, Any]) -> list[str] | None:
    tags = scene.get("tags") or []

    names = [
        tag.get("name").lower()
        for tag in tags
        if isinstance(tag, dict) and tag.get("name")
    ]

    return names or None


def _site(scene: dict[str, Any]) -> tuple[str | None, str | None]:
    site = scene.get("site")

    if isinstance(site, dict):
        site_id = site.get("uuid") or (
            str(site["id"]) if site.get("id") is not None else None
        )
        return site_id, site.get("name")

    return None, None


def scene_to_movie_dict(scene: dict[str, Any]) -> dict[str, Any]:
    """Map a TPDB scene JSON object to a Riven `Movie` init dict."""

    site_id, site_name = _site(scene)
    aired_at = parse_tpdb_date(scene.get("date"))

    return {
        "title": scene.get("title") or "Untitled",
        "poster_path": _poster(scene),
        "year": aired_at.year if aired_at else None,
        "tpdb_id": scene.get("id"),
        "site_id": site_id,
        "site_name": site_name,
        "performers": _performer_names(scene),
        "genres": _tag_names(scene),
        "aired_at": aired_at,
        "rating": scene.get("rating"),
        "content_rating": None,
        "type": "movie",
    }


def movie_to_movie_dict(movie: dict[str, Any]) -> dict[str, Any]:
    """Map a TPDB movie JSON object to a Riven `Movie` init dict."""

    return scene_to_movie_dict(movie)
