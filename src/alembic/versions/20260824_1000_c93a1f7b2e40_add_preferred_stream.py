"""Add preferred_stream_hash to MediaItem

Lets a user pin a specific release from the candidate list on the detail page,
so the downloader tries that one before its own quality ordering.

Revision ID: c93a1f7b2e40
Revises: 8c71d4e9a2f3
Create Date: 2026-08-24 10:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c93a1f7b2e40"
down_revision: Union[str, None] = "8c71d4e9a2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("MediaItem", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("preferred_stream_hash", sa.String(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("MediaItem", schema=None) as batch_op:
        batch_op.drop_column("preferred_stream_hash")
