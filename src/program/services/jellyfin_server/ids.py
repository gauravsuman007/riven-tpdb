"""Translation between Riven's integer ids and Jellyfin's GUID-shaped ones.

Jellyfin clients treat item ids as opaque, but many of them assume the 32-hex
shape a .NET Guid serialises to and will mangle or reject anything else. Our
ids are `MediaItem.id` integers, so they are widened into that shape rather
than a mapping table being kept: the encoding is reversible, which means no
state to persist, nothing to invalidate, and an id that survives a restart.
"""

# Fixed ids for the things that are not library items. Jellyfin wants a GUID
# for the server, the user and each "view" (the rows on the client's home
# screen); none of those correspond to a row in our database, so they are
# constants rather than derived.
SERVER_ID = "72697665-6e74-7064-6200-000000000001"
USER_ID = "72697665-6e74-7064-6200-000000000002"
LIBRARY_ID = "72697665-6e74-7064-6200-000000000003"

# High bit set, so a synthetic id can never collide with a real MediaItem id
# widened by `to_guid`. Used for People and Studios, which we surface as
# browsable entities but do not store as rows anywhere.
_SYNTHETIC_PREFIX = "ffffffff"


def to_guid(item_id: int) -> str:
    """Widen a MediaItem id into the 32-hex shape clients expect."""

    return f"{item_id:032x}"


def from_guid(guid: str) -> int | None:
    """Recover a MediaItem id, or None if this is not one of ours.

    Returns None rather than raising: the id arrives from a client over the
    network, and a malformed one is a 404, not a 500.
    """

    cleaned = guid.replace("-", "").strip()

    if not cleaned or cleaned.startswith(_SYNTHETIC_PREFIX):
        return None

    try:
        value = int(cleaned, 16)
    except ValueError:
        return None

    return value or None


def synthetic_guid(kind: str, name: str) -> str:
    """A stable id for a Person or Studio, which have no row of their own.

    Derived from the name so that the same performer keeps the same id across
    requests and restarts -- clients cache these and will show duplicates if
    the id moves.
    """

    from hashlib import blake2b

    digest = blake2b(f"{kind}:{name}".lower().encode(), digest_size=12).hexdigest()

    return f"{_SYNTHETIC_PREFIX}{digest}"
