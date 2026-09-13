"""A page's rows become something the user arranges.

Only the arrangement is stored -- page, rail key, order, on/off. What a rail
IS stays with whoever draws it, so a retitled row is retitled everywhere at
once instead of keeping a stale copy in whatever layouts happen to exist.

Nothing is seeded. An empty layout means "never arranged", which the pages
read as "use your own defaults"; seeding today's rows would freeze the
current defaults for every deployment and turn every later addition into a
row nobody sees.

Revision ID: f3b7d1e85c26
Revises: e2a6b8c04d17
"""

import sqlalchemy as sa
from alembic import op


revision = "f3b7d1e85c26"
down_revision = "e2a6b8c04d17"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "RailLayout",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("page", sa.String(), nullable=False),
        sa.Column("rail_key", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("page", "rail_key", name="uq_rail_page_key"),
    )
    op.create_index("ix_RailLayout_page", "RailLayout", ["page"])


def downgrade() -> None:
    op.drop_index("ix_RailLayout_page", table_name="RailLayout")
    op.drop_table("RailLayout")
