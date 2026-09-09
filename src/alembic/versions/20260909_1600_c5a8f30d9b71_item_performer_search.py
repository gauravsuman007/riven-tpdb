"""Index library cast and studio for search

Revision ID: c5a8f30d9b71
Revises: b7d4e5f61c92
Create Date: 2026-09-09 16:00:00.000000

Unnests `MediaItem.performers` into a searchable table and adds trigram
indexes so the library search box can match a studio or a cast member by
substring. Both the table and the indexes are derived data: dropping them
loses nothing that cannot be rebuilt from `MediaItem`.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c5a8f30d9b71"
down_revision: Union[str, None] = "b7d4e5f61c92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TRIGRAM_STATEMENTS = (
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    'CREATE INDEX IF NOT EXISTS ix_item_performer_name_trgm '
    'ON "ItemPerformer" USING gin (name_normalized gin_trgm_ops)',
    'CREATE INDEX IF NOT EXISTS ix_mediaitem_site_name_trgm '
    'ON "MediaItem" USING gin (lower(site_name) gin_trgm_ops)',
    'CREATE INDEX IF NOT EXISTS ix_mediaitem_title_trgm '
    'ON "MediaItem" USING gin (lower(title) gin_trgm_ops)',
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        "ItemPerformer",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("media_item_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("name_normalized", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["media_item_id"], ["MediaItem.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "media_item_id", "name_normalized", name="uq_item_performer_item_name"
        ),
    )
    op.create_index(
        "ix_item_performer_media_item_id", "ItemPerformer", ["media_item_id"]
    )
    op.create_index(
        "ix_item_performer_name_normalized", "ItemPerformer", ["name_normalized"]
    )

    if not _is_postgres():
        return

    connection = op.get_bind()

    # Backfill from the JSON column. `jsonb_array_elements_text` errors on a
    # non-array, and the column is free-form JSON, so the value is guarded
    # twice: the type check in the WHERE, and the lateral join only running
    # for rows that survive it.
    connection.execute(
        sa.text(
            """
            INSERT INTO "ItemPerformer" (media_item_id, name, name_normalized)
            SELECT DISTINCT ON (i.id, lower(btrim(p.value)))
                   i.id,
                   btrim(p.value),
                   lower(btrim(p.value))
              FROM "MediaItem" i
              CROSS JOIN LATERAL jsonb_array_elements_text(i.performers::jsonb) AS p(value)
             WHERE i.performers IS NOT NULL
               AND jsonb_typeof(i.performers::jsonb) = 'array'
               AND btrim(p.value) <> ''
            """
        )
    )

    # Trigram indexes are what make a mid-word match ("ril" -> "Riley Reid")
    # cheap. The extension needs rights the application user may not have on
    # every deployment, so a failure here degrades to a sequential scan over
    # a few thousand short rows rather than failing the upgrade -- search
    # still works, it is only unindexed.
    #
    # TRAP: `env.py` runs migrations with isolation_level="AUTOCOMMIT", so
    # there is no transaction to wrap these in and `begin_nested()` (a
    # SAVEPOINT) raises before the statement is even sent -- which is exactly
    # how the first deployment of this migration created none of the four.
    # AUTOCOMMIT is also why a plain try/except is enough: a failed statement
    # commits nothing and poisons nothing.
    for statement in _TRIGRAM_STATEMENTS:
        try:
            connection.execute(sa.text(statement))
        except Exception:  # noqa: BLE001 - see comment above
            continue


def downgrade() -> None:
    if _is_postgres():
        connection = op.get_bind()

        for index in (
            "ix_mediaitem_title_trgm",
            "ix_mediaitem_site_name_trgm",
            "ix_item_performer_name_trgm",
        ):
            connection.execute(sa.text(f'DROP INDEX IF EXISTS {index}'))

    op.drop_index("ix_item_performer_name_normalized", table_name="ItemPerformer")
    op.drop_index("ix_item_performer_media_item_id", table_name="ItemPerformer")
    op.drop_table("ItemPerformer")
