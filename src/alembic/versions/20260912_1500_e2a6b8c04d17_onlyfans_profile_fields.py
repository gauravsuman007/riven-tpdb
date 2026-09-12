"""Add the onlyfans.com profile fields to the performer index.

The picture, bio and counts now come from the performer's own profile via the
platform's own guest API (`program.services.onlyfans.profile`), so the index
needs somewhere to put them.

`of_checked_at` is CLEARED here on purpose. It records "we already looked for
this account's public profile", and everything it recorded was written by the
previous attempt, which read the site's single-page shell and could never
return per-account data. Leaving those stamps in place would make the new pass
skip precisely the accounts the old one had already failed on -- which is all
of them.

Revision ID: e2a6b8c04d17
Revises: d1f4a7c92b18
"""

import sqlalchemy as sa
from alembic import op


revision = "e2a6b8c04d17"
down_revision = "d1f4a7c92b18"
branch_labels = None
depends_on = None


_COLUMNS = (
    ("avatar_from_site", sa.Boolean(), False, sa.text("false")),
    ("of_user_id", sa.String(), True, None),
    ("of_username", sa.String(), True, None),
    ("header_url", sa.String(), True, None),
    ("website", sa.String(), True, None),
    ("location", sa.String(), True, None),
    ("is_verified", sa.Boolean(), False, sa.text("false")),
    ("posts_count", sa.Integer(), True, None),
    ("photos_count", sa.Integer(), True, None),
    ("videos_count", sa.Integer(), True, None),
    ("likes_count", sa.Integer(), True, None),
    ("subscribe_price", sa.Float(), True, None),
)


def upgrade() -> None:
    for name, type_, nullable, default in _COLUMNS:
        op.add_column(
            "OnlyFansAccount",
            sa.Column(name, type_, nullable=nullable, server_default=default),
        )

    op.execute('UPDATE "OnlyFansAccount" SET of_checked_at = NULL')

    # Every avatar already in the index came from an archive site -- its
    # thumbnail of the performer, or a still from their newest video there --
    # and the flag exists to let the performer's OWN profile picture replace
    # it. Marking the existing rows is what opens them to that upgrade; the
    # flag is named for the common case, not the only one.
    op.execute(
        'UPDATE "OnlyFansAccount" SET avatar_from_site = true '
        "WHERE avatar_url IS NOT NULL"
    )


def downgrade() -> None:
    for name, _type, _nullable, _default in reversed(_COLUMNS):
        op.drop_column("OnlyFansAccount", name)
