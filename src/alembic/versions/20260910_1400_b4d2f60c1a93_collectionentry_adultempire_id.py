"""A collection entry needs a column for an Adult Empire match.

`external_id` already holds an Adult Empire product number, but only for
entries that *came from* Adult Empire -- it is the source's own identity for a
self-sourced row, and the request and scrape paths address the entry by it.
An award entry that a *lookup* resolved to an Adult Empire product is a
different fact, and writing it to `external_id` would claim the row came from
a catalogue it never came from.

Without this column `assign_provider_id` refused to store the match at all --
correctly, since guessing a column is worse than recording nothing -- so
Adult Empire could resolve a library item but never a collection entry.

Revision ID: b4d2f60c1a93
Revises: a1c7e93b8d40
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b4d2f60c1a93"
down_revision: Union[str, None] = "a1c7e93b8d40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "CollectionEntry", sa.Column("adultempire_id", sa.String(), nullable=True)
    )
    op.create_index(
        "ix_collectionentry_adultempire_id", "CollectionEntry", ["adultempire_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_collectionentry_adultempire_id", table_name="CollectionEntry")
    op.drop_column("CollectionEntry", "adultempire_id")
