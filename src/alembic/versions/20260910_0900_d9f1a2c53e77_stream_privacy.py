"""Record which tracker a stream came from, and how private it is.

A debrid service joins the swarm from its own network with no account
anywhere, so a release on a private or semi-private tracker is unreachable to
it however many seeders the indexer reports. Storing the indexer's declared
privacy is what lets that be explained instead of appearing as an
indefinitely stalled download.

Revision ID: d9f1a2c53e77
Revises: e2b7c40a9d16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d9f1a2c53e77"
down_revision: Union[str, None] = "e2b7c40a9d16"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable with no backfill: every row that predates this genuinely does
    # not know, and "public" would be a guess that reads as a fact.
    op.add_column("Stream", sa.Column("privacy", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("Stream", "privacy")
