"""Adult Empire studios, savable and enriched from TPDB.

Only the studios are stored, not their titles. A studio's rows are read live
from the storefront in whichever order the page is asking for; mirroring two
ranked listings for each of a hundred studios would rebuild twenty thousand
rows a week to serve pages that are mostly never opened.

The artwork columns are TPDB's, because Adult Empire's studio pages carry a
name and a result count and nothing else -- no description, no logo.

Revision ID: a4c9e2f81b73
Revises: d3f8b1e57a24
Create Date: 2026-08-26 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a4c9e2f81b73"
down_revision: Union[str, None] = "d3f8b1e57a24"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "Studio",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ae_id", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("title_count", sa.Integer(), nullable=True),
        sa.Column("tpdb_site_id", sa.String(), nullable=True),
        sa.Column("logo_path", sa.String(), nullable=True),
        sa.Column("poster_path", sa.String(), nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        # server_default so the column is usable on existing rows the moment
        # it exists, rather than depending on the ORM default for a backfill.
        sa.Column(
            "saved", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("saved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("refreshed_at", sa.DateTime(), nullable=True),
        sa.Column("tpdb_checked_at", sa.DateTime(), nullable=True),
    )

    op.create_index("ix_studio_ae_id", "Studio", ["ae_id"], unique=True)
    op.create_index("ix_studio_name", "Studio", ["name"])
    op.create_index("ix_studio_tpdb_site_id", "Studio", ["tpdb_site_id"])
    # The brochure's studios section reads exactly this predicate on every
    # page load, and it is a tiny slice of the table.
    op.create_index("ix_studio_saved", "Studio", ["saved"])


def downgrade() -> None:
    op.drop_index("ix_studio_saved", table_name="Studio")
    op.drop_index("ix_studio_tpdb_site_id", table_name="Studio")
    op.drop_index("ix_studio_name", table_name="Studio")
    op.drop_index("ix_studio_ae_id", table_name="Studio")
    op.drop_table("Studio")
