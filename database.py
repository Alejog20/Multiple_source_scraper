"""
database.py — Persistence Layer
================================
Owns all schema definitions and async session management for the scraper
service. The engine URL is driven by the DATABASE_URL environment variable,
which means the entire storage backend can be swapped from SQLite (development)
to PostgreSQL (production) by changing a single env var — no application code
needs to change.

Design decisions:
    - SQLAlchemy 2.x async API (create_async_engine + AsyncSession) keeps the
      persistence layer non-blocking so it never stalls the Telegram event loop.
    - `expire_on_commit=False` on the session factory prevents SQLAlchemy from
      invalidating ORM object attributes after a commit. In async code, accessing
      expired attributes would trigger implicit lazy-loads that raise
      MissingGreenlet errors. Disabling expiry lets us safely pass detached
      objects around after a session closes.
    - JSON-encoded Text columns for `platforms` and `execution_times` are used
      instead of SQLAlchemy's JSON type because SQLite's JSON support is limited
      and inconsistent across driver versions. When migrating to PostgreSQL,
      these columns can be changed to `JSON` type without altering any helper
      methods on the model — the encode/decode API stays the same.
"""

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncGenerator, Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# ---------------------------------------------------------------------------
# Engine & Session Factory
# ---------------------------------------------------------------------------

DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "sqlite+aiosqlite:///./scraper_jobs.db",
)
"""Connection string read from the environment at import time.

Default: SQLite via aiosqlite (zero-config, file-based).
Production override example::

    DATABASE_URL=postgresql+asyncpg://user:pass@host/dbname python bot.py
"""

_echo_sql: bool = os.getenv("DB_ECHO", "false").lower() == "true"
"""Set DB_ECHO=true in the environment to print all generated SQL statements.

Useful during development; must be disabled in production to avoid leaking
sensitive query parameters into logs.
"""

engine = create_async_engine(DATABASE_URL, echo=_echo_sql)
"""Async engine instance. Module-level singleton — shared across the whole
process so connection pool resources are reused efficiently.
"""

async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    expire_on_commit=False,
)
"""Factory that produces AsyncSession instances.

`expire_on_commit=False` is set intentionally — see module docstring for the
full explanation. Every DB operation in this service should use
:func:`session_scope` rather than calling this factory directly.
"""


# ---------------------------------------------------------------------------
# ORM Base
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    """SQLAlchemy 2.x declarative base.

    All ORM models inherit from this class. Using the new-style
    ``DeclarativeBase`` (rather than the legacy ``declarative_base()``
    function) enables full Python type-checker support via ``Mapped[T]``
    annotations.
    """


# ---------------------------------------------------------------------------
# ORM Models
# ---------------------------------------------------------------------------


class TrackedJob(Base):
    """Represents a single price-tracking job created by a Telegram user.

    Each job stores what to search, which platforms to search, when to run,
    and when to expire. The scheduler reads this table at startup to restore
    any jobs that were active before the last process restart (idempotency).

    Attributes:
        id: Auto-incrementing primary key.
        chat_id: Telegram chat ID as a string. Stored as string rather than
            integer to future-proof against Telegram changing ID formats and
            to avoid sign/size issues with very large chat IDs.
        query: The search term entered by the user (e.g., "iPhone 15 Pro").
        platforms: JSON-encoded list of platform names to search.
            Example stored value: ``'["amazon", "mercadolibre"]'``.
            Use :meth:`get_platforms` to decode.
        schedule_type: One of ``"daily"``, ``"weekly"``, or
            ``"custom_days"``. Determines how the APScheduler trigger is
            configured when the job is (re-)registered.
        execution_times: JSON-encoded list of ``"HH:MM"`` strings.
            Example: ``'["08:00", "20:00"]'``. Use :meth:`get_execution_times`
            to decode.
        expiration_date: UTC datetime after which the job should be deactivated
            and the user notified that tracking has ended.
        is_active: ``True`` while the job is running. Set to ``False`` when
            the job expires or the user cancels it. Inactive jobs are ignored
            by the scheduler but kept for historical/audit purposes.
        created_at: Timestamp set by the database server at INSERT time.
        history: Back-reference to all :class:`ScrapeHistory` rows for this
            job.
    """

    __tablename__ = "tracked_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    query: Mapped[str] = mapped_column(String(512), nullable=False)
    platforms: Mapped[str] = mapped_column(Text, nullable=False)
    schedule_type: Mapped[str] = mapped_column(String(32), nullable=False)
    execution_times: Mapped[str] = mapped_column(Text, nullable=False)
    expiration_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    interval_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    # Relationship: one job → many history records.
    # `cascade="all, delete-orphan"` ensures history rows are removed when
    # a job is deleted, preventing orphaned records from accumulating.
    history: Mapped[list["ScrapeHistory"]] = relationship(
        "ScrapeHistory",
        back_populates="job",
        cascade="all, delete-orphan",
        lazy="select",
    )

    # ------------------------------------------------------------------
    # JSON encode/decode helpers
    # ------------------------------------------------------------------
    # SQLite has no native JSON column type with guaranteed round-trip
    # fidelity across all driver versions. Storing lists as JSON strings
    # in Text columns is the most portable approach. These helpers keep the
    # serialisation logic in one place so it's easy to swap to a proper
    # JSON column when moving to PostgreSQL.

    def get_platforms(self) -> list[str]:
        """Decode the platforms JSON string to a Python list.

        Returns:
            A list of platform name strings, e.g. ``["amazon", "mercadolibre"]``.
        """
        return json.loads(self.platforms)

    def get_execution_times(self) -> list[str]:
        """Decode the execution_times JSON string to a Python list.

        Returns:
            A list of ``"HH:MM"`` time strings, e.g. ``["08:00", "20:00"]``.
        """
        return json.loads(self.execution_times)

    @staticmethod
    def encode_platforms(platforms: list[str]) -> str:
        """Encode a list of platform names to a JSON string for storage.

        Args:
            platforms: Platform name strings, e.g. ``["amazon"]``.

        Returns:
            JSON-encoded string suitable for the ``platforms`` column.
        """
        return json.dumps(platforms)

    @staticmethod
    def encode_times(times: list[str]) -> str:
        """Encode a list of ``"HH:MM"`` strings to a JSON string for storage.

        Args:
            times: List of time strings, e.g. ``["08:00", "20:00"]``.

        Returns:
            JSON-encoded string suitable for the ``execution_times`` column.
        """
        return json.dumps(times)

    def __repr__(self) -> str:
        return (
            f"TrackedJob(id={self.id!r}, chat_id={self.chat_id!r}, "
            f"query={self.query!r}, is_active={self.is_active!r})"
        )


class ScrapeHistory(Base):
    """Records the outcome of a single scheduled scrape execution.

    A new row is written after every execution of a :class:`TrackedJob`,
    regardless of success or partial failure. This gives users and developers
    a full audit trail of what was found (and when), and allows computing
    price trends over time in future versions.

    Attributes:
        id: Auto-incrementing primary key.
        job_id: Foreign key to the :class:`TrackedJob` that triggered this
            run. Cascades on delete so history is cleaned up when a job is
            removed.
        total_found: Total number of products returned across all platforms
            after deduplication. May be 0 if all scrapers failed.
        lowest_price: The lowest numeric price found in this run, or ``None``
            if no products with a valid price were returned (e.g., all prices
            were missing / all scrapers errored out).
        timestamp: UTC timestamp set by the database at INSERT time.
        job: Back-reference to the parent :class:`TrackedJob`.
    """

    __tablename__ = "scrape_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("tracked_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    total_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lowest_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    job: Mapped["TrackedJob"] = relationship("TrackedJob", back_populates="history")

    def __repr__(self) -> str:
        return (
            f"ScrapeHistory(id={self.id!r}, job_id={self.job_id!r}, "
            f"total_found={self.total_found!r}, lowest_price={self.lowest_price!r})"
        )


# ---------------------------------------------------------------------------
# Database Initialisation
# ---------------------------------------------------------------------------


def _apply_migrations(conn) -> None:
    """Run lightweight schema migrations on an existing database.

    SQLAlchemy's ``create_all`` only creates missing *tables* — it never
    ALTERs columns on tables that already exist.  When a new nullable column
    is added to a model (e.g. ``interval_days``), any database file created
    before that change needs a one-time ``ALTER TABLE`` to gain the column.

    SQLite does not support ``ADD COLUMN IF NOT EXISTS``, so we probe the
    column list first and skip the statement if the column is already there.
    This function is synchronous because it is called via ``run_sync``.
    """
    existing = {
        row[1]
        for row in conn.execute(
            text("PRAGMA table_info(tracked_jobs)")
        ).fetchall()
    }
    if "interval_days" not in existing:
        conn.execute(
            text("ALTER TABLE tracked_jobs ADD COLUMN interval_days INTEGER")
        )


async def init_db() -> None:
    """Create all database tables if they do not already exist.

    This function is idempotent: calling it multiple times (e.g., on every
    bot restart) is safe because SQLAlchemy emits ``CREATE TABLE IF NOT EXISTS``
    statements. It should be called once during application startup, before the
    bot begins accepting messages or the scheduler starts dispatching jobs.

    Example::

        # In bot.py application startup handler:
        async def on_startup(application):
            await init_db()
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_apply_migrations)


# ---------------------------------------------------------------------------
# Session Context Manager
# ---------------------------------------------------------------------------


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Async context manager that provides a transactional database session.

    Wraps every database operation in a transaction that is automatically
    committed on success and rolled back on any exception. Callers never
    need to call ``session.commit()`` or ``session.rollback()`` manually,
    which eliminates a whole class of bugs where developers forget to commit
    or fail to rollback on error paths.

    Usage::

        async with session_scope() as session:
            job = TrackedJob(chat_id="123", query="laptop", ...)
            session.add(job)
            # commit happens automatically when the block exits cleanly

        # On exception, rollback is automatic — the exception re-raises
        # so the caller can handle it (e.g., notify the user).

    Yields:
        An :class:`~sqlalchemy.ext.asyncio.AsyncSession` bound to an open
        transaction.

    Raises:
        Re-raises any exception that occurs inside the ``with`` block after
        rolling back the transaction.
    """
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
