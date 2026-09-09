"""Track titles kept on the local download path

Revision ID: b7d4e5f61c92
Revises: a3c9e21d7b48
Create Date: 2026-09-09 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7d4e5f61c92"
down_revision: Union[str, None] = "a3c9e21d7b48"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "LocalCopy",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("media_item_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=True),
        sa.Column("bytes_done", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("bytes_total", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["media_item_id"], ["MediaItem.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        # One copy per title: "keep" is a property of the title, not of a
        # particular release, so a candidate switch must not create a second.
        sa.UniqueConstraint("media_item_id", name="uq_local_copy_media_item_id"),
    )
    op.create_index("ix_local_copy_media_item_id", "LocalCopy", ["media_item_id"])
    op.create_index("ix_local_copy_state", "LocalCopy", ["state"])


def downgrade() -> None:
    op.drop_index("ix_local_copy_state", table_name="LocalCopy")
    op.drop_index("ix_local_copy_media_item_id", table_name="LocalCopy")
    op.drop_table("LocalCopy")
