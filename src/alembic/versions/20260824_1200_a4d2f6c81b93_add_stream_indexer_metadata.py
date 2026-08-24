"""Record what the indexer said about a release.

Seeders, leechers, size and indexer name were reported by every scraper and
discarded at the scraper boundary. Persisting them lets the UI explain why a
release is stalled instead of only that it is.

Revision ID: a4d2f6c81b93
Revises: c93a1f7b2e40
Create Date: 2026-08-24 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a4d2f6c81b93"
down_revision: Union[str, None] = "c93a1f7b2e40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable throughout: existing rows were scraped before this data was
    # kept, and "unknown" is the truthful value for them. Backfilling zeros
    # would make every historic release look dead.
    op.add_column("Stream", sa.Column("seeders", sa.Integer(), nullable=True))
    op.add_column("Stream", sa.Column("leechers", sa.Integer(), nullable=True))
    op.add_column("Stream", sa.Column("size", sa.BigInteger(), nullable=True))
    op.add_column("Stream", sa.Column("indexer", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("Stream", "indexer")
    op.drop_column("Stream", "size")
    op.drop_column("Stream", "leechers")
    op.drop_column("Stream", "seeders")
