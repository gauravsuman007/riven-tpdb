"""The one place a persisted timestamp is created or serialized.

Every `datetime.now()` in this codebase used to be naive -- no tzinfo, computed
in whatever TZ the container happened to be configured with (`America/New_York`
on this deployment). Stored in a non-tz-aware column and serialized with a bare
`.isoformat()` (no `Z`, no offset), a value like that is genuinely ambiguous:
the frontend's `new Date(...)` has no way to know it wasn't already local to
the browser, and silently renders it off by whatever the two timezones differ
by. `utcnow()` and `to_iso_utc()` are the fix -- write and serialize UTC only,
explicitly, everywhere a timestamp crosses a boundary (DB write, JSON response).
"""

from datetime import datetime, timezone


def utcnow() -> datetime:
    """The only way a new persisted timestamp should be created."""

    return datetime.now(timezone.utc)


def to_iso_utc(value: datetime | None) -> str | None:
    """A timestamp as JSON should see it: ISO-8601, UTC, trailing `Z`.

    A naive `value` is assumed to already be UTC (true for every column this
    module's `utcnow()` writes to) rather than re-interpreted in the server's
    local TZ -- guessing a different zone here would just move the ambiguity
    from write time to read time.
    """

    if value is None:
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
