"""Adult Empire as an independent source of requestable titles.

Two halves. MediaItem gains adultempire_id so a title can enter the library
and be downloaded without a TPDB record ever existing for it -- the brochure
supplies title, studio, year and cast, which is all the scrapers need.

CollectionEntry gains the fields a ranked storefront listing has and an award
ballot does not: rank, an audience rating, a source-native id, and runtime.
external_id is what the request and scrape paths address a self-sourced entry
by, in place of tpdb_id.

Revision ID: d3f8b1e57a24
Revises: b7e4a2f19c05
Create Date: 2026-08-24 21:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "d3f8b1e57a24"
down_revision: Union[str, None] = "b7e4a2f19c05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("MediaItem", sa.Column("adultempire_id", sa.String(), nullable=True))
    op.create_index("ix_mediaitem_adultempire_id", "MediaItem", ["adultempire_id"])

    op.add_column(
        "CollectionEntry", sa.Column("external_source", sa.String(), nullable=True)
    )
    op.add_column(
        "CollectionEntry", sa.Column("external_id", sa.String(), nullable=True)
    )
    # Nullable: an award ballot has no ranking, and inventing one (say, by row
    # order) would read as meaningful when it is not.
    op.add_column("CollectionEntry", sa.Column("rank", sa.Integer(), nullable=True))
    op.add_column("CollectionEntry", sa.Column("rating", sa.Float(), nullable=True))
    op.add_column(
        "CollectionEntry", sa.Column("duration_minutes", sa.Integer(), nullable=True)
    )
    op.add_column(
        "CollectionEntry", sa.Column("released_at", sa.DateTime(), nullable=True)
    )

    op.create_index(
        "ix_collectionentry_external_source", "CollectionEntry", ["external_source"]
    )
    op.create_index("ix_collectionentry_external_id", "CollectionEntry", ["external_id"])


def downgrade() -> None:
    op.drop_index("ix_collectionentry_external_id", table_name="CollectionEntry")
    op.drop_index("ix_collectionentry_external_source", table_name="CollectionEntry")
    op.drop_column("CollectionEntry", "released_at")
    op.drop_column("CollectionEntry", "duration_minutes")
    op.drop_column("CollectionEntry", "rating")
    op.drop_column("CollectionEntry", "rank")
    op.drop_column("CollectionEntry", "external_id")
    op.drop_column("CollectionEntry", "external_source")

    op.drop_index("ix_mediaitem_adultempire_id", table_name="MediaItem")
    op.drop_column("MediaItem", "adultempire_id")
