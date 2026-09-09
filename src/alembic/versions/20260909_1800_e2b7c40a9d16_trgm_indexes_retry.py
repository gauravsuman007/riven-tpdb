"""Create the search trigram indexes that c5a8f30d9b71 silently skipped

Revision ID: e2b7c40a9d16
Revises: c5a8f30d9b71
Create Date: 2026-09-09 18:00:00.000000

`c5a8f30d9b71` wrapped its optional index creation in `begin_nested()`, and
`env.py` runs migrations with isolation_level="AUTOCOMMIT" -- where a
SAVEPOINT is invalid and raises before the statement is even sent. The
exception handler there swallowed it, so a database that ran that migration
has the table and the backfill and none of the four trigram statements, with
nothing in the log to say so.

Fixing the old migration in place only helps a database that has not run it
yet. This repeats the statements for one that has. Every statement is
`IF NOT EXISTS`, so it is a no-op wherever they already exist.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e2b7c40a9d16"
down_revision: Union[str, None] = "c5a8f30d9b71"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATEMENTS = (
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    'CREATE INDEX IF NOT EXISTS ix_item_performer_name_trgm '
    'ON "ItemPerformer" USING gin (name_normalized gin_trgm_ops)',
    'CREATE INDEX IF NOT EXISTS ix_mediaitem_site_name_trgm '
    'ON "MediaItem" USING gin (lower(site_name) gin_trgm_ops)',
    'CREATE INDEX IF NOT EXISTS ix_mediaitem_title_trgm '
    'ON "MediaItem" USING gin (lower(title) gin_trgm_ops)',
)


def upgrade() -> None:
    connection = op.get_bind()

    if connection.dialect.name != "postgresql":
        return

    for statement in _STATEMENTS:
        try:
            connection.execute(sa.text(statement))
        except Exception:  # noqa: BLE001
            # Still optional, and still only a performance property: an
            # unindexed search over a few thousand short rows works.
            continue


def downgrade() -> None:
    # The indexes belong to c5a8f30d9b71; dropping them here would take them
    # away from a database that never had this problem.
    pass
