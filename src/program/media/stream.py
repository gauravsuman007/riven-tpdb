from typing import TYPE_CHECKING, Any

import sqlalchemy
from RTN import Torrent
from sqlalchemy import Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from program.db.base_model import Base


if TYPE_CHECKING:
    from program.media.item import MediaItem
    # Imported for typing only: the scrapers package imports
    # program.media.item, which imports this module.
    from program.services.scrapers.results import ScrapeResult


class StreamRelation(Base):
    __tablename__ = "StreamRelation"

    id: Mapped[int] = mapped_column(sqlalchemy.Integer, primary_key=True)
    parent_id: Mapped[int] = mapped_column(
        sqlalchemy.ForeignKey("MediaItem.id", ondelete="CASCADE")
    )
    child_id: Mapped[int] = mapped_column(
        sqlalchemy.ForeignKey("Stream.id", ondelete="CASCADE")
    )

    __table_args__ = (
        Index("ix_streamrelation_parent_id", "parent_id"),
        Index("ix_streamrelation_child_id", "child_id"),
    )


class StreamBlacklistRelation(Base):
    __tablename__ = "StreamBlacklistRelation"

    id: Mapped[int] = mapped_column(sqlalchemy.Integer, primary_key=True)
    media_item_id: Mapped[int] = mapped_column(
        sqlalchemy.ForeignKey("MediaItem.id", ondelete="CASCADE")
    )
    stream_id: Mapped[int] = mapped_column(
        sqlalchemy.ForeignKey("Stream.id", ondelete="CASCADE")
    )

    __table_args__ = (
        Index("ix_streamblacklistrelation_media_item_id", "media_item_id"),
        Index("ix_streamblacklistrelation_stream_id", "stream_id"),
    )


class Stream(Base):
    __tablename__ = "Stream"

    id: Mapped[int] = mapped_column(sqlalchemy.Integer, primary_key=True)
    infohash: Mapped[str]
    raw_title: Mapped[str]
    parsed_title: Mapped[str]
    rank: Mapped[int]
    lev_ratio: Mapped[float]
    resolution: Mapped[str | None]
    # What the indexer said about the release. All nullable: a scraper that
    # does not report a field leaves it unknown rather than claiming zero,
    # which for `seeders` is the difference between "slow" and "dead".
    seeders: Mapped[int | None] = mapped_column(sqlalchemy.Integer, nullable=True)
    leechers: Mapped[int | None] = mapped_column(sqlalchemy.Integer, nullable=True)
    size: Mapped[int | None] = mapped_column(sqlalchemy.BigInteger, nullable=True)
    indexer: Mapped[str | None] = mapped_column(sqlalchemy.String, nullable=True)
    privacy: Mapped[str | None] = mapped_column(sqlalchemy.String, nullable=True)
    """"public", "semiPrivate" or "private" -- see `ScrapeResult.privacy`.

    Stored rather than looked up at display time because an indexer can be
    reconfigured or removed, and a stream already in the database still needs
    to explain why it never downloaded.
    """
    download_url: Mapped[str | None] = mapped_column(sqlalchemy.String, nullable=True)
    """The indexer's .torrent link -- see `ScrapeResult.download_url`.

    Stored because the fetch happens later than the scrape, and re-running the
    search to recover a URL we already had would be both slow and unreliable
    (indexer results move).
    """
    is_cached: bool = False
    parents: Mapped[list["MediaItem"]] = relationship(
        secondary="StreamRelation", back_populates="streams", lazy="selectin"
    )
    blacklisted_parents: Mapped[list["MediaItem"]] = relationship(
        secondary="StreamBlacklistRelation",
        back_populates="blacklisted_streams",
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_stream_infohash", "infohash"),
        Index("ix_stream_raw_title", "raw_title"),
        Index("ix_stream_parsed_title", "parsed_title"),
        Index("ix_stream_rank", "rank"),
        Index("ix_stream_resolution", "resolution"),
    )

    def __init__(self, torrent: Torrent, result: "ScrapeResult | None" = None):
        self.raw_title = torrent.raw_title
        self.infohash = torrent.infohash
        self.parsed_title = torrent.data.parsed_title
        self.parsed_data = torrent.data
        self.rank = torrent.rank
        self.lev_ratio = torrent.lev_ratio
        self.resolution = (
            torrent.data.resolution.lower() if torrent.data.resolution else "unknown"
        )
        self.is_cached = False
        # is_cached is handled by its default value in the mapped_column definition

        # `result` is optional so existing callers that only have a ranked
        # Torrent keep working; they simply get a stream with no indexer
        # metadata rather than a TypeError.
        if result is not None:
            self.seeders = result.seeders
            self.leechers = result.leechers
            self.size = result.size
            self.indexer = result.indexer
            self.privacy = result.privacy
            self.download_url = result.download_url

    def __hash__(self):
        return hash(self.infohash)

    def __eq__(self, other: Any):
        return isinstance(other, Stream) and self.infohash == other.infohash

    def to_dict(self):
        """Convert stream to dictionary for API serialization"""

        return {
            "id": self.id,
            "infohash": self.infohash,
            "raw_title": self.raw_title,
            "parsed_title": self.parsed_title,
            "rank": self.rank,
            "lev_ratio": self.lev_ratio,
            "resolution": self.resolution,
            "seeders": self.seeders,
            "leechers": self.leechers,
            "size": self.size,
            "indexer": self.indexer,
            "privacy": self.privacy,
            # The URL itself is NOT serialised: it carries the Prowlarr API
            # key. Callers only need to know whether one exists.
            "has_torrent_file": bool(self.download_url),
            # Surfaced so a person picking a release can see the difference
            # between "few seeders" and "seeders your debrid service can
            # never reach". They are not the same problem and only one of
            # them is fixable by waiting.
            "debrid_reachable": self.privacy in (None, "public"),
            "is_cached": self.is_cached,
        }
