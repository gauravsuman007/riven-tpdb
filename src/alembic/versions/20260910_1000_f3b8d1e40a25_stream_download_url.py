"""Keep the indexer's .torrent link alongside the infohash.

Riven hands the debrid provider a bare magnet with no trackers, so a swarm
that announces only to its own tracker is invisible to it. The torrent file
carries the announce list, and the provider accepts one directly -- measured
on a real release, this is the difference between "stalled (no seeds)" and
4.3 MB/s.

Revision ID: f3b8d1e40a25
Revises: d9f1a2c53e77
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f3b8d1e40a25"
down_revision: Union[str, None] = "d9f1a2c53e77"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("Stream", sa.Column("download_url", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("Stream", "download_url")
