import asyncio

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, async_engine_from_config

from alembic import context

from ai_os.core.db.models import Base
from ai_os.core.persistence import default_db_url

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Deliberately NOT calling `fileConfig(config.config_file_name)` here (the
# alembic-init default): `init_db()` runs this on every AI-OS startup, not
# just a standalone `alembic upgrade` CLI invocation, and `fileConfig`
# defaults to `disable_existing_loggers=True` — it would silently disable
# every logger AI-OS itself had already configured (e.g.
# `ai_os.mcp.adapters.gemini_adapter`'s runtime warnings) on the very first
# `Persistence.open()` call of the process.

# Only used for the standalone `alembic upgrade head` CLI path (no live
# connection handed in via config.attributes) — mirrors the same
# AI_OS_HOME-aware default the CLI/`Persistence.open` use, instead of a
# hardcoded path in alembic.ini that would drift from it.
if not config.attributes.get("connection"):
    config.set_main_option("sqlalchemy.url", default_db_url())

# `ai_os.core.db.models.Base` is the single source of truth for the schema —
# `--autogenerate` diffs against this, and `upgrade head` from an empty
# database produces exactly what `Base.metadata.create_all` used to.
target_metadata = Base.metadata


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


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    Two entry points share this function: the plain `alembic upgrade head`
    CLI (no pre-existing connection — builds its own engine from
    `alembic.ini`'s `sqlalchemy.url`) and `ai_os.core.db.database.init_db`,
    which is already inside a live `AsyncEngine`/`AsyncConnection` and hands
    it in via `config.attributes["connection"]` so migrations run on that
    same connection instead of opening a second one (needed for the
    `sqlite+aiosqlite:///:memory:` case, where a second connection would be a
    distinct, empty database — see `database.py`'s own note on this).
    """
    connectable = config.attributes.get("connection", None)

    if connectable is None:
        asyncio.run(run_async_migrations())
        return

    if isinstance(connectable, AsyncEngine):
        raise RuntimeError(
            "alembic env.py received an AsyncEngine via config.attributes["
            "'connection'] — pass a live AsyncConnection instead (see "
            "database.py's `run_migrations`), since env.py always needs a "
            "connection already inside the caller's event loop/transaction."
        )

    do_run_migrations(connectable)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
