"""Curated collections that sit beside the library rather than inside it.

A CollectionEntry is a catalogue row, not a library item. It carries the source
metadata and, once resolved, a TPDB id -- but it only gains a MediaItem when
someone actually requests it. That nullable ``media_item_id`` is what lets an
11,000-entry award corpus exist without putting 11,000 titles in the library.

Revision ID: b7e4a2f19c05
Revises: a4d2f6c81b93
Create Date: 2026-08-24 15:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b7e4a2f19c05"
down_revision: Union[str, None] = "a4d2f6c81b93"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "Collection",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("poster_path", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("refreshed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_collection_key", "Collection", ["key"], unique=True)
    op.create_index("ix_collection_source", "Collection", ["source"])
    op.create_index("ix_collection_year", "Collection", ["year"])
    op.create_index("ix_collection_source_year", "Collection", ["source", "year"])

    op.create_table(
        "CollectionEntry",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("collection_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("studio", sa.String(), nullable=True),
        sa.Column("performers", sa.JSON(), nullable=True),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("winner", sa.Boolean(), nullable=False),
        sa.Column("tpdb_id", sa.String(), nullable=True),
        sa.Column("tpdb_kind", sa.String(), nullable=True),
        sa.Column("match_state", sa.String(), nullable=False),
        sa.Column("match_score", sa.Float(), nullable=True),
        sa.Column("matched_at", sa.DateTime(), nullable=True),
        sa.Column("poster_path", sa.String(), nullable=True),
        sa.Column("media_item_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["collection_id"], ["Collection.id"], ondelete="CASCADE"
        ),
        # SET NULL rather than CASCADE: deleting a requested title from the
        # library should return the entry to "not requested", not erase the
        # award record itself.
        sa.ForeignKeyConstraint(
            ["media_item_id"], ["MediaItem.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "collection_id", "title", "category", name="uq_collectionentry_slot"
        ),
    )
    op.create_index("ix_collectionentry_collection_id", "CollectionEntry", ["collection_id"])
    op.create_index("ix_collectionentry_tpdb_id", "CollectionEntry", ["tpdb_id"])
    op.create_index("ix_collectionentry_winner", "CollectionEntry", ["winner"])
    op.create_index("ix_collectionentry_match_state", "CollectionEntry", ["match_state"])
    op.create_index(
        "ix_collectionentry_state", "CollectionEntry", ["collection_id", "match_state"]
    )
    op.create_index("ix_collectionentry_media_item_id", "CollectionEntry", ["media_item_id"])


def downgrade() -> None:
    op.drop_table("CollectionEntry")
    op.drop_table("Collection")
