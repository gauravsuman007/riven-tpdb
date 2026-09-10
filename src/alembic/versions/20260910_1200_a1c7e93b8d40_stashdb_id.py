"""A second metadata provider needs its own identifier columns.

A StashDB UUID is not a TPDB id. Sharing `tpdb_id` between them would make
every lookup, dedupe and "already in the library" check quietly wrong, with no
way afterwards to tell which provider a value came from.

Revision ID: a1c7e93b8d40
Revises: f3b8d1e40a25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1c7e93b8d40"
down_revision: Union[str, None] = "f3b8d1e40a25"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("MediaItem", sa.Column("stashdb_id", sa.String(), nullable=True))
    op.create_index("ix_mediaitem_stashdb_id", "MediaItem", ["stashdb_id"])

    op.add_column(
        "CollectionEntry", sa.Column("stashdb_id", sa.String(), nullable=True)
    )
    op.create_index(
        "ix_collectionentry_stashdb_id", "CollectionEntry", ["stashdb_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_collectionentry_stashdb_id", table_name="CollectionEntry")
    op.drop_column("CollectionEntry", "stashdb_id")

    op.drop_index("ix_mediaitem_stashdb_id", table_name="MediaItem")
    op.drop_column("MediaItem", "stashdb_id")
