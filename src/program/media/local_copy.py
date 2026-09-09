"""Tracking for library titles kept on local disk.

A title normally lives only in the debrid account: RivenVFS presents it as a
file, but every byte is fetched on demand and nothing is stored. A "keep"
copies one title's active file to a directory on this server, so it survives
the debrid account expiring, the provider deleting the torrent, or the
internet being down.

The copy is deliberately tracked in its own table rather than as a flag on
`FilesystemEntry`: it has a lifecycle of its own (queued, copying, on disk,
failed), it holds bytes-so-far for the progress the UI shows, and it must be
able to outlive the entry it was made from -- replacing a release with a
better candidate should not silently invalidate a file already on disk.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

import sqlalchemy
from sqlalchemy.orm import Mapped, mapped_column, relationship

from program.db.base_model import Base

if TYPE_CHECKING:
    from program.media.item import MediaItem


class LocalCopyState(str, Enum):
    """Where a title's local copy has got to.

    Named for what the user sees on the button, not for internal mechanics:
    the UI shows "Keep on disk" with no copy, "Queued", "Syncing NN%", "On
    disk", or "Failed".
    """

    Queued = "Queued"
    Syncing = "Syncing"
    OnDisk = "OnDisk"
    Failed = "Failed"


class LocalCopy(Base):
    """One title's copy on the local download path."""

    __tablename__ = "LocalCopy"

    id: Mapped[int] = mapped_column(
        sqlalchemy.Integer, primary_key=True, autoincrement=True
    )

    media_item_id: Mapped[int] = mapped_column(
        sqlalchemy.Integer,
        sqlalchemy.ForeignKey("MediaItem.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    media_item: Mapped["MediaItem"] = relationship(
        "MediaItem", back_populates="local_copy", lazy="selectin"
    )

    state: Mapped[str] = mapped_column(
        sqlalchemy.String,
        nullable=False,
        default=LocalCopyState.Queued.value,
    )

    # Absolute path on this server, inside the configured local download path.
    # Recorded even while syncing so a cancel knows what to delete.
    path: Mapped[str | None] = mapped_column(sqlalchemy.String, nullable=True)

    bytes_done: Mapped[int] = mapped_column(
        sqlalchemy.BigInteger, nullable=False, default=0
    )
    bytes_total: Mapped[int] = mapped_column(
        sqlalchemy.BigInteger, nullable=False, default=0
    )

    # Why the last attempt failed, shown in the UI. Cleared on a retry.
    error: Mapped[str | None] = mapped_column(sqlalchemy.String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        sqlalchemy.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        sqlalchemy.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        sqlalchemy.DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        sqlalchemy.Index("ix_local_copy_media_item_id", "media_item_id"),
        sqlalchemy.Index("ix_local_copy_state", "state"),
    )

    def __repr__(self) -> str:
        return (
            f"<LocalCopy(item={self.media_item_id}, state={self.state}, "
            f"{self.bytes_done}/{self.bytes_total})>"
        )

    @property
    def percent(self) -> float:
        """Completion 0-100. Zero rather than a divide-by-zero before size is known."""

        if not self.bytes_total:
            return 0.0

        return round(min(100.0, self.bytes_done * 100.0 / self.bytes_total), 1)

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "path": self.path,
            "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
            "percent": self.percent,
            "error": self.error,
            "updated_at": (
                self.updated_at.astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
                if self.updated_at
                else None
            ),
            "completed_at": (
                self.completed_at.astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
                if self.completed_at
                else None
            ),
        }
