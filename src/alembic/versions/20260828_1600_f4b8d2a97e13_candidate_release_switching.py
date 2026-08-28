"""Add columns for background candidate-release download and switch

Revision ID: f4b8d2a97e13
Revises: e7a4c1f83b52
Create Date: 2026-08-28 16:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f4b8d2a97e13"
down_revision: Union[str, None] = "e7a4c1f83b52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "FilesystemEntry",
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )
    op.add_column(
        "MediaEntry",
        sa.Column("stream_infohash", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_media_entry_stream_infohash", "MediaEntry", ["stream_infohash"]
    )
    op.add_column(
        "MediaItem",
        sa.Column("downloading_stream_hash", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("MediaItem", "downloading_stream_hash")
    op.drop_index("ix_media_entry_stream_infohash", table_name="MediaEntry")
    op.drop_column("MediaEntry", "stream_infohash")
    op.drop_column("FilesystemEntry", "is_active")
