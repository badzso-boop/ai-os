"""Async SQLAlchemy 2.0 engine/session plumbing (doc 13 + doc 14).

Deliberately does NOT copy doc 13/14's example code verbatim — that blueprint has
two real gaps that matter once you actually run it against SQLite:

1. In-memory URLs (``sqlite+aiosqlite:///:memory:``) need ``StaticPool`` +
   ``connect_args={"check_same_thread": False}``. Without this, the async
   engine's default pool opens a *fresh* DBAPI connection per checkout, and
   each ``:memory:`` connection is a wholly separate, empty database — a
   session that writes and a session that reads back would silently see two
   different databases. A real file-backed URL does not need this: every
   connection opens the same file, so normal pooling is fine (and it's what
   you want for concurrency).

2. The SQLite PRAGMAs (`journal_mode=WAL`, `foreign_keys=ON`) must be applied
   via a ``sqlalchemy.event.listen`` "connect" hook targeting
   ``engine.sync_engine`` — not the ``AsyncEngine`` wrapper itself, which does
   not support the sync event system directly. SQLite disables foreign-key
   enforcement per-connection by default, so skipping this would let a
   dangling ``TaskModel.epic_id`` reference an epic that doesn't exist,
   silently, even though the ORM declares the relationship.

Note: ``PRAGMA journal_mode=WAL`` is a no-op on ``:memory:`` databases — SQLite
stays in ``memory`` journal mode regardless of what you ask for there. That's
expected and is not something callers need to work around; just don't assert
WAL mode against an in-memory engine.

Alembic/migrations are intentionally out of scope for this MVP cut — there is
no prior schema to migrate from yet.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from ai_os.core.db.models import Base

__all__ = ["make_engine", "init_db", "make_sessionmaker"]


def _is_memory_url(url: str) -> bool:
    """True for SQLite URLs that resolve to an in-process, transient database.

    Covers ``sqlite+aiosqlite:///:memory:`` (explicit) as well as
    ``sqlite+aiosqlite://`` / ``sqlite+aiosqlite:///`` (no path at all, which
    SQLite also treats as an anonymous in-memory database).
    """
    if ":memory:" in url:
        return True
    for prefix in ("sqlite+aiosqlite://", "sqlite://"):
        if url.startswith(prefix):
            remainder = url[len(prefix):]
            if remainder in ("", "/"):
                return True
    return False


def make_engine(url: str) -> AsyncEngine:
    """Build an async SQLAlchemy engine for the given SQLite URL.

    In-memory URLs get `StaticPool` + `check_same_thread=False` so every
    checkout reuses the *same* underlying connection (and thus the same
    in-memory database) instead of each session silently getting its own
    empty one. File-backed URLs use the default async pool.

    Every connection (memory or file) gets `PRAGMA foreign_keys=ON` so
    dangling foreign keys raise, and `PRAGMA journal_mode=WAL` so concurrent
    readers don't block a writer on a real file-backed database (a no-op on
    `:memory:`).
    """
    engine_kwargs: dict[str, Any] = {}
    if _is_memory_url(url):
        engine_kwargs["poolclass"] = StaticPool
        engine_kwargs["connect_args"] = {"check_same_thread": False}

    engine = create_async_engine(url, **engine_kwargs)

    # Must target engine.sync_engine: AsyncEngine itself isn't a valid target
    # for sqlalchemy.event (the sync event system it wraps).
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


async def init_db(engine: AsyncEngine) -> None:
    """Create all tables declared on `Base.metadata`.

    Must be called explicitly (once at startup, or once per test) rather than
    at import time: `Base.metadata.create_all` needs a live connection, and
    the async engine only opens one inside an `async with`/`await` context.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build a session factory bound to `engine`.

    `expire_on_commit=False` so ORM instances (e.g. a freshly created
    `EpicModel`) stay usable after `commit()` without triggering an implicit
    (and, for async sessions, forbidden-outside-await) refresh.
    """
    return async_sessionmaker(engine, expire_on_commit=False)
