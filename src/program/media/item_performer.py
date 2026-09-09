"""The cast of a library title, one performer per row.

`MediaItem.performers` already holds the cast, but it holds it as a JSON
array, and a JSON array cannot be searched by substring: Postgres will match
an exact element with a GIN index and nothing else, so "type *ril*, see
*Riley Reid*" is impossible against the column itself. This table is that
column unnested, so a name is a row and a row can be indexed.

It is deliberately derived, never authored. Nothing writes here directly:
`sync_item_performers` is driven by mapper events on `MediaItem`, so every
path that sets `performers` -- the TPDB indexer, the Adult Empire indexer,
TPDB enrichment, and anything added later -- keeps the table correct by
construction rather than by remembering to call something. Rebuilding it from
`MediaItem.performers` is always safe.

Only the LIBRARY is indexed here. TPDB's catalogue is millions of titles and
is not the thing being searched; at roughly three performers per scene this
table is about three rows per owned title, which is why the size of it is not
a concern.
"""

import re
from typing import Any, Iterable

import sqlalchemy
from sqlalchemy import event
from sqlalchemy.orm import Mapped, mapped_column

from program.db.base_model import Base

_WHITESPACE = re.compile(r"\s+")


def normalise_name(name: str) -> str:
    """Fold a name to its search form: lowercase, single-spaced, trimmed.

    Matching is done against this, never against the display name, so
    "Riley  Reid " and "riley reid" are the same performer to a search while
    the row still carries the casing TPDB gave us for display.
    """

    return _WHITESPACE.sub(" ", name.strip().lower())


class ItemPerformer(Base):
    """One (title, performer) pair."""

    __tablename__ = "ItemPerformer"

    id: Mapped[int] = mapped_column(
        sqlalchemy.Integer, primary_key=True, autoincrement=True
    )

    media_item_id: Mapped[int] = mapped_column(
        sqlalchemy.Integer,
        sqlalchemy.ForeignKey("MediaItem.id", ondelete="CASCADE"),
        nullable=False,
    )

    #: As TPDB or the storefront spells it. This is what the UI shows.
    name: Mapped[str] = mapped_column(sqlalchemy.String, nullable=False)

    #: What searches and suggestions match on. See `normalise_name`.
    name_normalized: Mapped[str] = mapped_column(sqlalchemy.String, nullable=False)

    __table_args__ = (
        sqlalchemy.Index("ix_item_performer_media_item_id", "media_item_id"),
        sqlalchemy.Index("ix_item_performer_name_normalized", "name_normalized"),
        # A title listing the same performer twice is a data error upstream,
        # not something to carry into the suggestions.
        sqlalchemy.UniqueConstraint(
            "media_item_id", "name_normalized", name="uq_item_performer_item_name"
        ),
    )

    def __repr__(self) -> str:
        return f"<ItemPerformer(item={self.media_item_id}, name={self.name!r})>"


def clean_names(performers: Any) -> list[tuple[str, str]]:
    """(display, normalized) pairs for a `MediaItem.performers` value.

    Tolerant on purpose: the column is JSON, so it can legitimately hold
    None, and a bad upstream payload can put dicts or numbers in it. Anything
    that is not a usable name is dropped rather than raising -- a malformed
    cast list must not be able to fail the flush that saves the title.
    """

    if not isinstance(performers, (list, tuple)):
        return []

    seen: set[str] = set()
    cleaned: list[tuple[str, str]] = []

    for entry in performers:
        if not isinstance(entry, str):
            continue

        normalized = normalise_name(entry)

        if not normalized or normalized in seen:
            continue

        seen.add(normalized)
        cleaned.append((entry.strip(), normalized))

    return cleaned


def sync_item_performers(
    connection: sqlalchemy.engine.Connection,
    media_item_id: int,
    performers: Any,
) -> None:
    """Make this title's rows match `performers` exactly.

    Delete-then-insert rather than a diff: the cast of a title is a handful
    of rows, and a diff would be more code to get subtly wrong for no
    measurable gain.
    """

    table = ItemPerformer.__table__

    connection.execute(
        table.delete().where(table.c.media_item_id == media_item_id)
    )

    rows = [
        {"media_item_id": media_item_id, "name": name, "name_normalized": normalized}
        for name, normalized in clean_names(performers)
    ]

    if rows:
        connection.execute(table.insert(), rows)


def _register_events() -> None:
    """Keep the table in step with `MediaItem.performers`.

    Mapper events, not calls at the write sites: `performers` is set by the
    TPDB indexer, the Adult Empire indexer and TPDB enrichment today, and a
    fourth write site added later would silently stop updating suggestions.
    `propagate=True` covers the Movie/Show/Season/Episode subclasses.
    """

    from program.media.item import MediaItem

    @event.listens_for(MediaItem, "after_insert", propagate=True)
    def _after_insert(mapper, connection, target) -> None:  # type: ignore[no-untyped-def]
        if target.performers:
            sync_item_performers(connection, target.id, target.performers)

    @event.listens_for(MediaItem, "after_update", propagate=True)
    def _after_update(mapper, connection, target) -> None:  # type: ignore[no-untyped-def]
        # Items are updated constantly as they move through the pipeline.
        # Rewriting the cast on every state change would turn one UPDATE into
        # a delete and three inserts, so only a real change to the column
        # touches this table.
        history = sqlalchemy.inspect(target).attrs.performers.history

        if history.has_changes():
            sync_item_performers(connection, target.id, target.performers)


_register_events()


def rebuild_all(session) -> int:  # type: ignore[no-untyped-def]
    """Rebuild every row from `MediaItem.performers`. Returns rows written.

    The migration backfills in SQL; this exists for the case the table is
    ever suspected of having drifted, and as the thing the tests assert
    against.
    """

    from program.media.item import MediaItem

    table = ItemPerformer.__table__
    connection = session.connection()

    connection.execute(table.delete())

    written = 0
    rows: list[dict[str, Any]] = []

    items: Iterable[tuple[int, Any]] = session.execute(
        sqlalchemy.select(MediaItem.id, MediaItem.performers).where(
            MediaItem.performers.is_not(None)
        )
    ).all()

    for item_id, performers in items:
        for name, normalized in clean_names(performers):
            rows.append(
                {
                    "media_item_id": item_id,
                    "name": name,
                    "name_normalized": normalized,
                }
            )

    if rows:
        connection.execute(table.insert(), rows)
        written = len(rows)

    return written
