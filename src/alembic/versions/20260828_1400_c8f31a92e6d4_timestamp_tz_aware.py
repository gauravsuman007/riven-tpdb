"""Make persisted timestamps timezone-aware.

Every timestamp column in this app used to be a naive `TIMESTAMP WITHOUT TIME
ZONE`, written from `datetime.now()` -- which, on this deployment, runs in the
container's configured `America/New_York` (see docker-compose.yml `TZ=`).
Serialized with a bare `.isoformat()` (no offset, no `Z`), that value is
genuinely ambiguous to any client: `new Date(...)` on the frontend has no way
to know it was not already local, and silently renders it off by whatever the
two zones differ by.

Two exceptions to the "assume America/New_York" rule used for every other
column here:

* `FilesystemEntry.created_at`/`updated_at` already wrote
  `datetime.now(timezone.utc)` -- correct UTC values, just stored in a
  non-tz-aware column. These are reinterpreted as UTC, not shifted.
* `CollectionEntry.released_at` is a release date parsed from external source
  metadata (Adult Empire / awards data), never a `datetime.now()` write. Left
  as UTC (no shift) rather than guessed at, since it was never local wall-clock
  to begin with.

`AT TIME ZONE` used with a source offset **removes** that offset and re-adds
UTC, so `TIMESTAMP WITHOUT TIME ZONE AT TIME ZONE 'America/New_York'`
correctly reinterprets old Eastern-wall-clock values as the real UTC instant
they represented, rather than just relabeling the same numbers as UTC (which
would leave every existing timestamp off by the DST-dependent UTC offset).

Revision ID: c8f31a92e6d4
Revises: a4c9e2f81b73
Create Date: 2026-08-28 14:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c8f31a92e6d4"
down_revision: Union[str, None] = "a4c9e2f81b73"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table, column, source_tz) -- source_tz is the zone the existing naive value
# is assumed to already be wall-clock in.
_EASTERN_COLUMNS = [
    ("MediaItem", "requested_at"),
    ("MediaItem", "indexed_at"),
    ("MediaItem", "scraped_at"),
    ("Studio", "saved_at"),
    ("Studio", "created_at"),
    ("Studio", "refreshed_at"),
    ("Studio", "tpdb_checked_at"),
    ("Collection", "created_at"),
    ("Collection", "refreshed_at"),
    ("CollectionEntry", "matched_at"),
    ("ScheduledTask", "scheduled_for"),
    ("ScheduledTask", "created_at"),
    ("ScheduledTask", "executed_at"),
]

# Already-correct UTC values, stored naive -- reinterpret as UTC, do not shift.
_UTC_COLUMNS = [
    ("FilesystemEntry", "created_at"),
    ("FilesystemEntry", "updated_at"),
    ("CollectionEntry", "released_at"),
    # MediaItem.aired_at is external release-date metadata, never a
    # datetime.now() write -- same "already correct, do not shift" treatment.
    ("MediaItem", "aired_at"),
]


def _to_tz_aware(table: str, column: str, source_tz: str) -> None:
    op.execute(
        f'ALTER TABLE "{table}" '
        f'ALTER COLUMN "{column}" TYPE TIMESTAMP WITH TIME ZONE '
        f'USING "{column}" AT TIME ZONE \'{source_tz}\''
    )


def _to_tz_aware_no_shift(table: str, column: str) -> None:
    op.execute(
        f'ALTER TABLE "{table}" '
        f'ALTER COLUMN "{column}" TYPE TIMESTAMP WITH TIME ZONE '
        f'USING "{column}" AT TIME ZONE \'UTC\''
    )


def upgrade() -> None:
    for table, column in _EASTERN_COLUMNS:
        _to_tz_aware(table, column, "America/New_York")

    for table, column in _UTC_COLUMNS:
        _to_tz_aware_no_shift(table, column)


def downgrade() -> None:
    for table, column in _EASTERN_COLUMNS:
        op.execute(
            f'ALTER TABLE "{table}" '
            f'ALTER COLUMN "{column}" TYPE TIMESTAMP WITHOUT TIME ZONE '
            f'USING "{column}" AT TIME ZONE \'America/New_York\''
        )

    for table, column in _UTC_COLUMNS:
        op.execute(
            f'ALTER TABLE "{table}" '
            f'ALTER COLUMN "{column}" TYPE TIMESTAMP WITHOUT TIME ZONE '
            f'USING "{column}" AT TIME ZONE \'UTC\''
        )
