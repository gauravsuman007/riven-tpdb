"""Lock manually-decided TPDB associations against the automatic matcher

Revision ID: a3c9e21d7b48
Revises: f4b8d2a97e13
Create Date: 2026-08-30

Clearing a wrong TPDB match did not stick. The enrichment pass selects
candidates on ``tpdb_id IS NULL``, so detaching an item only put it back in
the queue, and the next run re-attached the same wrong record. Observed
directly: a title detached by hand was back on a 2011 release of a
different film within the hour.

``tpdb_locked`` marks an association a person decided -- set OR cleared --
and the matcher skips those.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a3c9e21d7b48"
down_revision: Union[str, None] = "f4b8d2a97e13"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default so existing rows get a real value rather than NULL under
    # a NOT NULL column; the ORM default covers new ones.
    op.add_column(
        "MediaItem",
        sa.Column(
            "tpdb_locked",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("MediaItem", "tpdb_locked")
