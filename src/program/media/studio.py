"""Adult Empire studios, mirrored locally so they can be browsed and saved.

A studio is deliberately *not* a :class:`~program.media.collection.Collection`.
A collection is a fixed list of titles that a sync rebuilds wholesale; a studio
is a catalogue that can be sorted several ways, and its rows used to be read
live from the storefront on every request -- several seconds per page load,
serialised behind a one-request-a-second courtesy delay.

``StudioRowEntry`` is the one deliberate exception to "never stored": the top
`N` (default 25) titles of each row, but *only* for studios with ``saved=True``
-- the two or three a user actually follows, not the full ~1,200-studio
directory. That bound is what keeps this from becoming the "twenty thousand
rows rebuilt weekly" outcome the live-read design was built to avoid: capped
per studio, and only for studios someone opted into. Refreshed weekly
alongside the directory sync, by :meth:`StudioService.sync_rows`.

What is stored otherwise is the studio itself, because the list of studios is
the part that is slow to obtain (three sitemaps plus a TPDB lookup each) and
almost never changes.

``saved`` is what the brochure's studios section lists. The full hundred is a
directory to pick from, not a shelf: showing all of them would bury the two or
three a user actually follows, which is the same reasoning that keeps award
years out of the library's Collections shelf.
"""

from datetime import datetime

import sqlalchemy
from sqlalchemy.orm import Mapped, mapped_column

from program.db.base_model import Base
from program.utils.time import utcnow


class Studio(Base):
    """One studio on Adult Empire, optionally enriched from TPDB."""

    __tablename__ = "Studio"

    id: Mapped[int] = mapped_column(sqlalchemy.Integer, primary_key=True)

    # Adult Empire's numeric studio id, from the URL and the page's data-tid.
    # This is the identity: the slug changes with a rename, the id does not.
    ae_id: Mapped[str] = mapped_column(sqlalchemy.String, unique=True, index=True)

    # The URL slug differs per catalogue ("-porn-movies", "-porn-videos"), so
    # it is stored as seen rather than derived from the name.
    slug: Mapped[str | None] = mapped_column(sqlalchemy.String, nullable=True)
    name: Mapped[str] = mapped_column(sqlalchemy.String, index=True)

    # How many titles the storefront lists. Useful for ordering the directory,
    # and a cheap signal that a studio is worth following at all.
    title_count: Mapped[int | None] = mapped_column(
        sqlalchemy.Integer, nullable=True
    )

    # From TPDB, which is the only source that has studio artwork -- Adult
    # Empire's studio pages carry a name and a result count and nothing else.
    # All nullable: not every storefront studio exists on TPDB.
    tpdb_site_id: Mapped[str | None] = mapped_column(
        sqlalchemy.String, nullable=True, index=True
    )
    logo_path: Mapped[str | None] = mapped_column(sqlalchemy.String, nullable=True)
    poster_path: Mapped[str | None] = mapped_column(sqlalchemy.String, nullable=True)
    description: Mapped[str | None] = mapped_column(sqlalchemy.String, nullable=True)

    # Set by the user, cleared by the user. Never touched by a sync: a weekly
    # refresh that dropped someone's saved studios would be indistinguishable
    # from data loss.
    saved: Mapped[bool] = mapped_column(
        sqlalchemy.Boolean, default=False, index=True, server_default="false"
    )
    saved_at: Mapped[datetime | None] = mapped_column(
        sqlalchemy.DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        sqlalchemy.DateTime(timezone=True), default=utcnow
    )
    refreshed_at: Mapped[datetime | None] = mapped_column(
        sqlalchemy.DateTime(timezone=True), nullable=True
    )
    # Separate from refreshed_at so a studio TPDB has never heard of is not
    # re-looked-up on every weekly sync.
    tpdb_checked_at: Mapped[datetime | None] = mapped_column(
        sqlalchemy.DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        return f"<Studio {self.name} ({self.ae_id})>"


class StudioRowEntry(Base):
    """One cached title in one of a saved studio's ranked rows.

    Keyed by ``(studio_id, sort, rank)`` rather than ``(studio_id, sort,
    product_id)`` -- a rank position is what a weekly refresh overwrites, and
    two rows for the same studio and sort should never both claim rank 1.
    """

    __tablename__ = "StudioRowEntry"

    id: Mapped[int] = mapped_column(sqlalchemy.Integer, primary_key=True)
    studio_id: Mapped[int] = mapped_column(
        sqlalchemy.ForeignKey("Studio.id", ondelete="CASCADE"), index=True
    )
    # "bestseller" / "trending" -- see STUDIO_SORTS in adultempire.py.
    sort: Mapped[str] = mapped_column(sqlalchemy.String, index=True)
    rank: Mapped[int] = mapped_column(sqlalchemy.Integer)

    product_id: Mapped[str] = mapped_column(sqlalchemy.String)
    title: Mapped[str] = mapped_column(sqlalchemy.String)
    poster: Mapped[str | None] = mapped_column(sqlalchemy.String, nullable=True)

    refreshed_at: Mapped[datetime] = mapped_column(
        sqlalchemy.DateTime(timezone=True), default=utcnow
    )

    __table_args__ = (
        sqlalchemy.UniqueConstraint(
            "studio_id", "sort", "rank", name="ux_studio_row_entry_position"
        ),
    )

    def __repr__(self) -> str:
        return f"<StudioRowEntry studio={self.studio_id} {self.sort}#{self.rank}>"
