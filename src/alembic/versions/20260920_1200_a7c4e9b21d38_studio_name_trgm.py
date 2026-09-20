"""Trigram index on the studio directory's name, for fuzzy search

Revision ID: a7c4e9b21d38
Revises: f3b7d1e85c26
Create Date: 2026-09-20 12:00:00.000000

`GET /api/v1/studios?search=` stopped being a substring test and became a
fuzzy match (`program.utils.fuzzy`), so it now asks Postgres for
`word_similarity(..., lower(name))`. Without an index that is a sequential scan of
the directory -- about 1,200 rows today, so it is quick either way, which is
exactly why this must not be mistaken for a correctness fix.

Same shape as `e2b7c40a9d16`, and for the same reasons: `env.py` runs
migrations under isolation_level="AUTOCOMMIT", where `begin_nested()` raises
before the statement is even sent. A plain try/except per statement is both
necessary and sufficient -- do not reach for a savepoint here.

Verify after deploying, because a swallowed failure looks exactly like
success:

    SELECT indexname FROM pg_indexes WHERE indexname LIKE '%trgm%';
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7c4e9b21d38"
down_revision: Union[str, None] = "f3b7d1e85c26"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATEMENTS = (
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    'CREATE INDEX IF NOT EXISTS ix_studio_name_trgm '
    'ON "Studio" USING gin (lower(name) gin_trgm_ops)',
)


def upgrade() -> None:
    connection = op.get_bind()

    if connection.dialect.name != "postgresql":
        return

    for statement in _STATEMENTS:
        try:
            connection.execute(sa.text(statement))
        except Exception:  # noqa: BLE001
            # A performance property only. The search is correct unindexed,
            # so a database that cannot create this is not a broken one.
            continue


def downgrade() -> None:
    connection = op.get_bind()

    if connection.dialect.name != "postgresql":
        return

    connection.execute(sa.text("DROP INDEX IF EXISTS ix_studio_name_trgm"))
