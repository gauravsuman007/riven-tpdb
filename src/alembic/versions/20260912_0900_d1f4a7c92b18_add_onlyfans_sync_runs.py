"""Add OnlyFansSyncRun

Per-site status for the index walk: what state it is in, how far it got, and
why it stopped. Written as the run goes, so it is also the progress readout.

Revision ID: d1f4a7c92b18
Revises: c8e1a5d73f92
Create Date: 2026-09-12 09:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "d1f4a7c92b18"
down_revision: Union[str, None] = "c8e1a5d73f92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "OnlyFansSyncRun",
        # The site key is the identity: one row per site, overwritten each
        # run. History is deliberately not kept -- the question this answers
        # is "where is it now", and a growing log would need its own pruning.
        sa.Column("site", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("accounts_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("accounts_new", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("site", name="pk_onlyfans_sync_run"),
    )


def downgrade() -> None:
    op.drop_table("OnlyFansSyncRun")
