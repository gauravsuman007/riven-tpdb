"""Curated collections and their entries.

A collection is a browsable list that lives beside the library without being
part of it. This distinction is the whole point of the model:

    * A :class:`CollectionEntry` is a *catalogue row* -- a title known to exist,
      with whatever metadata the source gave us. It costs nothing and is never
      scraped or downloaded.
    * ``media_item_id`` is set only once an entry has actually been requested.
      Until then the entry has no MediaItem, so it cannot appear in the library
      listing, cannot enter the pipeline, and cannot be counted as owned.

That is what keeps an 11,000-entry award corpus from turning into 11,000
library items. Entries are promoted to MediaItems individually, either by the
user or by an auto-request policy that targets a narrow subset (award winners).
"""

from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy
from sqlalchemy import Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from program.db.base_model import Base

if TYPE_CHECKING:
    from program.media.item import MediaItem


class Collection(Base):
    """A named list of titles, grouped under a source and usually a year."""

    __tablename__ = "Collection"

    id: Mapped[int] = mapped_column(sqlalchemy.Integer, primary_key=True)

    # Stable identifier built from source and year (``avn-2026``). Rebuilding a
    # collection targets this, so a refresh updates in place rather than
    # creating a duplicate.
    key: Mapped[str] = mapped_column(sqlalchemy.String, unique=True, index=True)

    source: Mapped[str] = mapped_column(sqlalchemy.String, index=True)
    name: Mapped[str] = mapped_column(sqlalchemy.String)
    description: Mapped[str | None] = mapped_column(sqlalchemy.String, nullable=True)
    year: Mapped[int | None] = mapped_column(sqlalchemy.Integer, nullable=True, index=True)
    poster_path: Mapped[str | None] = mapped_column(sqlalchemy.String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        sqlalchemy.DateTime, default=datetime.now
    )
    refreshed_at: Mapped[datetime | None] = mapped_column(
        sqlalchemy.DateTime, nullable=True
    )

    entries: Mapped[list["CollectionEntry"]] = relationship(
        "CollectionEntry",
        back_populates="collection",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    __table_args__ = (Index("ix_collection_source_year", "source", "year"),)

    def __repr__(self) -> str:
        return f"<Collection {self.key} ({len(self.entries)} entries)>"


class CollectionEntry(Base):
    """One title in a collection.

    ``tpdb_id`` is nullable on purpose: an entry is recorded whether or not it
    resolves to a TPDB record, so an unmatched award title stays visible as a
    known gap instead of vanishing. ``match_state`` says which it is.
    """

    __tablename__ = "CollectionEntry"

    id: Mapped[int] = mapped_column(sqlalchemy.Integer, primary_key=True)
    collection_id: Mapped[int] = mapped_column(
        sqlalchemy.ForeignKey("Collection.id", ondelete="CASCADE"), index=True
    )

    # Identity at the source, for catalogues that are self-sufficient. An
    # Adult Empire entry carries title, studio, year and cast already, so it is
    # requestable without ever resolving a TPDB id -- ``external_id`` is what
    # the request and scrape paths address it by.
    external_source: Mapped[str | None] = mapped_column(
        sqlalchemy.String, nullable=True, index=True
    )
    external_id: Mapped[str | None] = mapped_column(
        sqlalchemy.String, nullable=True, index=True
    )

    # Position in a ranked listing, 1-based. Null for unordered collections
    # such as an award ballot, where no ranking exists to record.
    rank: Mapped[int | None] = mapped_column(sqlalchemy.Integer, nullable=True)

    # Audience score at the source, on that source's own scale (Adult Empire
    # rates out of 5). Not comparable across sources, so it is stored raw
    # rather than normalised into something that looks universal.
    rating: Mapped[float | None] = mapped_column(sqlalchemy.Float, nullable=True)

    duration_minutes: Mapped[int | None] = mapped_column(
        sqlalchemy.Integer, nullable=True
    )

    # Exact release date where the source gives one. ``year`` alone is enough
    # for a feature, but the scrapers' date check is sharper with the real day.
    released_at: Mapped[datetime | None] = mapped_column(
        sqlalchemy.DateTime, nullable=True
    )

    # Source metadata, kept verbatim so a re-match can be retried later without
    # re-fetching the source.
    title: Mapped[str] = mapped_column(sqlalchemy.String)
    studio: Mapped[str | None] = mapped_column(sqlalchemy.String, nullable=True)
    performers: Mapped[list[str] | None] = mapped_column(sqlalchemy.JSON, nullable=True)
    category: Mapped[str | None] = mapped_column(sqlalchemy.String, nullable=True)
    year: Mapped[int | None] = mapped_column(sqlalchemy.Integer, nullable=True)

    # Whether this entry won its category, as opposed to being nominated.
    winner: Mapped[bool] = mapped_column(sqlalchemy.Boolean, default=False, index=True)

    # Resolution against TPDB.
    tpdb_id: Mapped[str | None] = mapped_column(sqlalchemy.String, nullable=True, index=True)
    tpdb_kind: Mapped[str | None] = mapped_column(sqlalchemy.String, nullable=True)
    match_state: Mapped[str] = mapped_column(
        sqlalchemy.String, default="pending", index=True
    )
    match_score: Mapped[float | None] = mapped_column(sqlalchemy.Float, nullable=True)
    matched_at: Mapped[datetime | None] = mapped_column(
        sqlalchemy.DateTime, nullable=True
    )

    # Artwork copied off the matched TPDB record so a collection page can be
    # rendered without a TPDB round trip per entry.
    poster_path: Mapped[str | None] = mapped_column(sqlalchemy.String, nullable=True)

    # Set only when the entry has been requested. A null here is what keeps the
    # entry out of the library.
    media_item_id: Mapped[int | None] = mapped_column(
        sqlalchemy.ForeignKey("MediaItem.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    collection: Mapped["Collection"] = relationship(
        "Collection", back_populates="entries"
    )
    media_item: Mapped["MediaItem | None"] = relationship("MediaItem", lazy="selectin")

    __table_args__ = (
        # One row per title per category: a title can legitimately appear in
        # several categories of the same ceremony, but not twice in one.
        UniqueConstraint(
            "collection_id", "title", "category", name="uq_collectionentry_slot"
        ),
        Index("ix_collectionentry_state", "collection_id", "match_state"),
    )

    @property
    def requested(self) -> bool:
        return self.media_item_id is not None

    @property
    def actionable(self) -> bool:
        """Whether this entry can be requested as it stands.

        Two ways to qualify, and they are genuinely different: a TPDB id means
        the indexer can resolve full metadata, while an ``external_id`` means
        the source already gave us enough (title, studio, year, cast) to start
        a download without asking anyone else first.
        """

        return bool(self.tpdb_id or self.external_id)

    def __repr__(self) -> str:
        return f"<CollectionEntry {self.title!r} [{self.match_state}]>"


# Match states, in the order an entry moves through them.
MATCH_PENDING = "pending"
MATCH_MATCHED = "matched"
MATCH_UNMATCHED = "unmatched"
MATCH_SKIPPED = "skipped"

# The source supplied enough metadata to act on, so no TPDB lookup is owed.
# Distinct from "matched", which asserts a TPDB record was actually found.
MATCH_SELF_SOURCED = "self_sourced"
