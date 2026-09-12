"""The migration environment every add-on shares.

Add-ons ship only a ``versions/`` directory. This env is the host's, pointed
at that directory by `program.addons.database.migration_config`, so an add-on
never has to carry alembic boilerplate -- and, more to the point, cannot get
the two settings below wrong.

Both exist to keep the add-on's migration state inside the add-on's own
schema. ``version_table_schema`` puts ``alembic_version`` there, which is what
makes ``DROP SCHEMA ... CASCADE`` a complete uninstall instead of one that
leaves a revision nothing can resolve and a host that will not start.
``include_schemas`` is what lets autogenerate see those tables at all.
"""

from alembic import context
from sqlalchemy import engine_from_config, pool, text


config = context.config
schema = config.get_main_option("addon_schema")

# The add-on's own metadata, handed over by the host rather than imported:
# this file has no way to know which add-on it is running for.
target_metadata = config.attributes.get("target_metadata")


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        version_table_schema=schema,
        include_schemas=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection", None)

    if connectable is None:
        connectable = engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )

    with connectable.connect() as connection:
        # Before configure, so that a migration written without an explicit
        # `schema=` still creates its table in the add-on's schema rather
        # than silently in `public` -- where the CASCADE could never reach it.
        connection.execute(text(f'SET search_path TO "{schema}"'))

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema=schema,
            include_schemas=True,
            # Everything an add-on owns is inside its schema, so anything
            # outside it is the host's and must be invisible here -- an
            # autogenerate that could see the host's tables would offer to
            # drop them.
            include_object=lambda obj, name, type_, reflected, compare_to: (
                getattr(obj, "schema", schema) == schema
                if type_ == "table"
                else True
            ),
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
