from datetime import datetime

import sqlalchemy
from sqlalchemy import MetaData, orm


class Base(orm.DeclarativeBase):
    """Base class for all database models"""

    # Every bare `Mapped[datetime]` column (no explicit mapped_column type) gets
    # a timezone-aware column by default. Explicit `mapped_column(sqlalchemy.DateTime(...))`
    # calls still need `timezone=True` set themselves -- this only covers
    # inferred columns, see program/utils/time.py for the write/serialize half
    # of this fix.
    type_annotation_map = {
        datetime: sqlalchemy.DateTime(timezone=True),
    }


def get_base_metadata() -> MetaData:
    """Get the Base metadata for Alembic migrations"""

    # Import models to register them with Base.metadata

    from program.media import (
        MediaItem,  # pyright: ignore[reportUnusedImport]
        FilesystemEntry,  # pyright: ignore[reportUnusedImport]
        StreamRelation,  # pyright: ignore[reportUnusedImport]
        StreamBlacklistRelation,  # pyright: ignore[reportUnusedImport]
        Stream,  # pyright: ignore[reportUnusedImport]
        Collection,  # pyright: ignore[reportUnusedImport]
        CollectionEntry,  # pyright: ignore[reportUnusedImport]
        Studio,  # pyright: ignore[reportUnusedImport]
        StudioRowEntry,  # pyright: ignore[reportUnusedImport]
    )
    from program.scheduling import (
        ScheduledTask,  # pyright: ignore[reportUnusedImport]
    )

    return Base.metadata
