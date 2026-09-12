"""Performer accounts mirrored from the OnlyFans archive sites.

Two tables rather than one because the same person appears on several sites
under different slugs. The account is the deduplicated person, keyed on a
collapsed handle; the source row is one site's copy of them and holds the slug
that site's URLs are actually built from.

The unique constraint on (account_id, site) is load-bearing: without it a
second run of the weekly sync inserts a duplicate source per site instead of
updating, and `source_count` silently becomes a count of sync runs.

Revision ID: c8e1a5d73f92
Revises: b4d2f60c1a93
Create Date: 2026-09-11 16:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c8e1a5d73f92"
down_revision: Union[str, None] = "b4d2f60c1a93"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "OnlyFansAccount",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("handle", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("avatar_url", sa.String(), nullable=True),
        sa.Column("bio", sa.String(), nullable=True),
        # server_default so the columns are usable on existing rows the moment
        # they exist, rather than depending on the ORM default for a backfill.
        sa.Column(
            "source_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "saved", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("saved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("refreshed_at", sa.DateTime(), nullable=True),
        sa.Column("of_checked_at", sa.DateTime(), nullable=True),
    )

    op.create_index(
        "ix_onlyfans_account_handle", "OnlyFansAccount", ["handle"], unique=True
    )
    op.create_index(
        "ix_onlyfans_account_display_name", "OnlyFansAccount", ["display_name"]
    )
    # The account grid orders on these two on every page load.
    op.create_index(
        "ix_onlyfans_account_source_count", "OnlyFansAccount", ["source_count"]
    )
    op.create_index("ix_onlyfans_account_saved", "OnlyFansAccount", ["saved"])

    op.create_table(
        "OnlyFansAccountSource",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("site", sa.String(), nullable=False),
        sa.Column("site_handle", sa.String(), nullable=False),
        sa.Column("page_url", sa.String(), nullable=False),
        sa.Column("video_count", sa.Integer(), nullable=True),
        sa.Column("image_count", sa.Integer(), nullable=True),
        sa.Column("refreshed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"], ["OnlyFansAccount.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("account_id", "site", name="ux_onlyfans_source_site"),
    )

    op.create_index(
        "ix_onlyfans_source_account_id", "OnlyFansAccountSource", ["account_id"]
    )
    op.create_index("ix_onlyfans_source_site", "OnlyFansAccountSource", ["site"])
    op.create_index(
        "ix_onlyfans_source_site_handle",
        "OnlyFansAccountSource",
        ["site", "site_handle"],
    )


def downgrade() -> None:
    op.drop_index("ix_onlyfans_source_site_handle", table_name="OnlyFansAccountSource")
    op.drop_index("ix_onlyfans_source_site", table_name="OnlyFansAccountSource")
    op.drop_index("ix_onlyfans_source_account_id", table_name="OnlyFansAccountSource")
    op.drop_table("OnlyFansAccountSource")

    op.drop_index("ix_onlyfans_account_saved", table_name="OnlyFansAccount")
    op.drop_index("ix_onlyfans_account_source_count", table_name="OnlyFansAccount")
    op.drop_index("ix_onlyfans_account_display_name", table_name="OnlyFansAccount")
    op.drop_index("ix_onlyfans_account_handle", table_name="OnlyFansAccount")
    op.drop_table("OnlyFansAccount")
