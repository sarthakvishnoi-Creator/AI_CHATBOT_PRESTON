from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from preston import models  # noqa: F401  (registers the tables on Base.metadata)
from preston.core.config import get_settings
from preston.core.db import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Populated by importing preston.models above.
target_metadata = Base.metadata

# The URL comes from the application's own Settings (PRESTON_DATABASE_URL)
# rather than alembic.ini, so no credential is ever committed. psycopg 3
# uses the same "postgresql+psycopg://" URL for sync and async engines, so
# no driver rewrite is needed for this synchronous migration runner.
_database_url = get_settings().database_url
if _database_url is None:
    raise RuntimeError(
        "PRESTON_DATABASE_URL is not configured; Alembic cannot run "
        "migrations without a database URL."
    )
config.set_main_option("sqlalchemy.url", str(_database_url))


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
