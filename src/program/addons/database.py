"""One Postgres schema per add-on, and the uninstall that makes possible.

The whole reason an add-on's tables live in their own schema rather than
beside the host's is this statement:

    DROP SCHEMA <key> CASCADE

Complete by construction. No enumerating tables, no wondering whether the
sync-run table was in the list, no orphaned indexes or sequences -- and,
because the add-on's ``alembic_version`` lives in that schema too, no
migration rows left behind to crash the next startup with a revision nothing
can resolve.

It is also why each add-on runs an independent migration chain instead of a
labelled branch off the host's. Branches share one version table: removing an
add-on then leaves a head alembic cannot resolve, and the failure surfaces as
a host that will not start, with nothing pointing at the folder that was
deleted. Separate chains cannot fail that way.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from loguru import logger
from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.pool import NullPool

# `program.db.db` is the MODULE; the SQLAlchemy wrapper is `program.db.db`
# the ATTRIBUTE on the package, and the submodule of the same name shadows it
# on a plain `from program.db import db`. Imported through the submodule,
# which re-exports it, so the name resolves to the wrapper either way.
from program.db.db import db


#: Where the shared env.py and script template live. Add-ons ship only a
#: `versions/` directory; everything alembic needs around it is the host's, so
#: an add-on cannot get `version_table_schema` wrong.
TEMPLATE_DIR = Path(__file__).parent / "alembic_template"


def ensure_schema(key: str) -> None:
    """Create the add-on's schema if it is not already there."""

    with db.engine.connect() as connection:
        connection.execution_options(isolation_level="AUTOCOMMIT").execute(
            text(f'CREATE SCHEMA IF NOT EXISTS "{key}"')
        )

    logger.debug(f"Addon {key}: schema ready")


def migration_config(key: str, versions: Path, metadata: MetaData | None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(TEMPLATE_DIR))
    config.set_main_option("version_locations", str(versions))
    config.set_main_option("addon_schema", key)
    config.attributes["target_metadata"] = metadata
    return config


def upgrade(key: str, versions: Path, metadata: MetaData | None) -> None:
    """Bring one add-on's schema up to its own head.

    Raising is the caller's problem on purpose: a failed add-on migration
    should disable that add-on, and must never be able to stop the host from
    starting. `loader` catches it and marks the add-on failed.
    """

    ensure_schema(key)

    config = migration_config(key, versions, metadata)

    # A THROWAWAY ENGINE, NOT THE APPLICATION'S.
    #
    # The migration sets `search_path` to the add-on's schema so that a
    # migration written without an explicit `schema=` still lands in the right
    # place. On a pooled connection that setting SURVIVES being returned to
    # the pool -- and the next thing to borrow it was the host, which then
    # could not see its own tables. It brought the whole application down with
    # `relation "MediaItem" does not exist` moments after an add-on loaded
    # successfully, which points at nothing.
    #
    # NullPool means every connection here is closed rather than returned, so
    # nothing this does can escape into the application's pool.
    engine = create_engine(
        # `str(engine.url)` REDACTS the password; this is the same URL with it
        # intact, which is why it is not simply str().
        db.engine.url.render_as_string(hide_password=False),
        poolclass=NullPool,
    )

    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        engine.dispose()

    logger.debug(f"Addon {key}: migrations up to date")


def create_all(key: str, metadata: MetaData) -> None:
    """Create the add-on's tables directly, for an add-on shipping none.

    An add-on with models but no migrations is a legitimate thing to be --
    the schema is disposable, so "create what is missing" is a complete
    answer for one that has never changed shape. The moment it needs to
    change shape it needs migrations, and the host says so rather than
    guessing at an ALTER.
    """

    ensure_schema(key)

    # Fully qualified by the metadata's own schema, so unlike `upgrade` this
    # needs no `search_path` and can safely use the application's engine.
    metadata.create_all(db.engine)


def purge(key: str) -> bool:
    """Delete everything the add-on stored. Irreversible.

    One statement, and it is the point of the whole design. Refuses the
    obvious catastrophes -- `public`, and anything that is not a plain
    identifier -- because this string is interpolated, and a schema name that
    could carry a quote could carry the rest of a statement with it.
    """

    if not key.isidentifier() or key in ("public", "information_schema"):
        logger.error(f"Refusing to drop schema {key!r}")
        return False

    try:
        with db.engine.connect() as connection:
            connection.execution_options(isolation_level="AUTOCOMMIT").execute(
                text(f'DROP SCHEMA IF EXISTS "{key}" CASCADE')
            )

        logger.warning(f"Addon {key}: schema dropped, all of its data is gone")
        return True
    except Exception as exc:
        logger.error(f"Addon {key}: could not drop schema: {exc}")
        return False


def schema_size(key: str) -> dict[str, int]:
    """Tables and total bytes, so the management page can say what a purge costs.

    "Remove this add-on" and "remove this add-on and its 6,402 rows" are
    different decisions, and the user can only tell them apart if the page
    knows the difference.
    """

    try:
        with db.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT count(*)::int AS tables, "
                    "COALESCE(sum(pg_total_relation_size("
                    "  quote_ident(schemaname) || '.' || quote_ident(relname)"
                    ")), 0)::bigint AS bytes "
                    "FROM pg_stat_user_tables WHERE schemaname = :schema"
                ),
                {"schema": key},
            ).first()

        return {"tables": row.tables if row else 0, "bytes": int(row.bytes) if row else 0}
    except Exception as exc:
        logger.debug(f"Addon {key}: could not measure schema: {exc}")
        return {"tables": 0, "bytes": 0}
