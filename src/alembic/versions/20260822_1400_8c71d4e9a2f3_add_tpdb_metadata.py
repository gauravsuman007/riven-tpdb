"""Add TPDB adult metadata fields

Revision ID: 8c71d4e9a2f3
Revises: b1345f835923
Create Date: 2026-08-22 14:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "8c71d4e9a2f3"
down_revision: Union[str, None] = "b1345f835923"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("MediaItem", schema=None) as batch_op:
        batch_op.add_column(sa.Column("tpdb_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("site_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("site_name", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("performers", sa.JSON(), nullable=True))

    op.create_index("ix_mediaitem_tpdb_id", "MediaItem", ["tpdb_id"])
    op.create_index("ix_mediaitem_site_id", "MediaItem", ["site_id"])


def downgrade() -> None:
    op.drop_index("ix_mediaitem_site_id", table_name="MediaItem")
    op.drop_index("ix_mediaitem_tpdb_id", table_name="MediaItem")

    with op.batch_alter_table("MediaItem", schema=None) as batch_op:
        batch_op.drop_column("performers")
        batch_op.drop_column("site_name")
        batch_op.drop_column("site_id")
        batch_op.drop_column("tpdb_id")