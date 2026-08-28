"""Cache the top N titles of each saved studio's ranked rows.

Bounded on purpose: only studios with `saved=True` get rows cached (the two or
three a user actually follows, not the ~1,200-studio directory), and only the
top `studio_rows_top_n` (default 25) of each row -- the exact scope that keeps
this from becoming the "twenty thousand rows rebuilt weekly" outcome the
original live-read design was built to avoid. See program/media/studio.py.

Revision ID: e7a4c1f83b52
Revises: c8f31a92e6d4
Create Date: 2026-08-28 15:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e7a4c1f83b52"
down_revision: Union[str, None] = "c8f31a92e6d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "StudioRowEntry",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("studio_id", sa.Integer(), nullable=False),
        sa.Column("sort", sa.String(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("poster", sa.String(), nullable=True),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["studio_id"], ["Studio.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "studio_id", "sort", "rank", name="ux_studio_row_entry_position"
        ),
    )
    op.create_index(
        "ix_studiorowentry_studio_id", "StudioRowEntry", ["studio_id"]
    )
    op.create_index("ix_studiorowentry_sort", "StudioRowEntry", ["sort"])


def downgrade() -> None:
    op.drop_index("ix_studiorowentry_sort", table_name="StudioRowEntry")
    op.drop_index("ix_studiorowentry_studio_id", table_name="StudioRowEntry")
    op.drop_table("StudioRowEntry")
