"""tests/test_database.py — Unit tests for the persistence layer.

Uses the isolated temp-file sqlite DB configured by tests/conftest.py (via
DATABASE_URL) so these tests never touch the real scraper_jobs.db. Migration
branches are exercised separately against a hand-built legacy schema.

Run with:
    uv run pytest tests/test_database.py -v
"""

import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

import database
from database import ScrapeHistory, ScrapedProduct, TrackedJob


@pytest.fixture(autouse=True, scope="module")
async def _ensure_tables():
    await database.init_db()


def _make_job(**overrides) -> TrackedJob:
    defaults = dict(
        chat_id="chat-1",
        query="laptop gaming",
        platforms=TrackedJob.encode_platforms(["amazon", "mercadolibre"]),
        schedule_type="daily",
        execution_times=TrackedJob.encode_times(["08:00"]),
        expiration_date=datetime.now(timezone.utc) + timedelta(days=30),
    )
    defaults.update(overrides)
    return TrackedJob(**defaults)


class TestTrackedJobHelpers:
    def test_encode_decode_platforms_roundtrip(self):
        encoded = TrackedJob.encode_platforms(["amazon", "mercadolibre"])
        job = _make_job(platforms=encoded)
        assert job.get_platforms() == ["amazon", "mercadolibre"]

    def test_encode_decode_times_roundtrip(self):
        encoded = TrackedJob.encode_times(["08:00", "20:00"])
        job = _make_job(execution_times=encoded)
        assert job.get_execution_times() == ["08:00", "20:00"]

    def test_repr_contains_key_fields(self):
        job = _make_job(chat_id="42", query="iphone")
        job.id = 7
        job.is_active = True
        text_repr = repr(job)
        assert "id=7" in text_repr
        assert "chat_id='42'" in text_repr
        assert "query='iphone'" in text_repr


class TestScrapeHistoryAndProductRepr:
    def test_scrape_history_repr(self):
        history = ScrapeHistory(id=1, job_id=2, total_found=5, lowest_price=9.99)
        r = repr(history)
        assert "id=1" in r
        assert "job_id=2" in r
        assert "total_found=5" in r

    def test_scraped_product_repr(self):
        product = ScrapedProduct(id=1, history_id=2, product_id="ASIN1", title="Widget")
        r = repr(product)
        assert "id=1" in r
        assert "history_id=2" in r
        assert "product_id='ASIN1'" in r


class TestSessionScopeAndCascade:
    async def test_commit_persists_job(self):
        async with database.session_scope() as session:
            job = _make_job(chat_id="commit-test")
            session.add(job)

        async with database.session_scope() as session:
            from sqlalchemy import select

            result = await session.execute(
                select(TrackedJob).where(TrackedJob.chat_id == "commit-test")
            )
            saved = result.scalar_one_or_none()
        assert saved is not None
        assert saved.query == "laptop gaming"

    async def test_rollback_on_exception(self):
        with pytest.raises(RuntimeError):
            async with database.session_scope() as session:
                job = _make_job(chat_id="rollback-test")
                session.add(job)
                await session.flush()
                raise RuntimeError("simulated failure mid-transaction")

        async with database.session_scope() as session:
            from sqlalchemy import select

            result = await session.execute(
                select(TrackedJob).where(TrackedJob.chat_id == "rollback-test")
            )
            saved = result.scalar_one_or_none()
        assert saved is None

    async def test_history_and_product_relationship_cascade(self):
        async with database.session_scope() as session:
            job = _make_job(chat_id="cascade-test")
            history = ScrapeHistory(total_found=1, lowest_price=10.0)
            product = ScrapedProduct(
                product_id="P1", title="Widget", price=10.0, source="Amazon"
            )
            history.products.append(product)
            job.history.append(history)
            session.add(job)
            await session.flush()
            job_id = job.id

        async with database.session_scope() as session:
            from sqlalchemy import select

            result = await session.execute(
                select(TrackedJob).where(TrackedJob.id == job_id)
            )
            saved_job = result.scalar_one()
            await session.refresh(saved_job, attribute_names=["history"])
            assert len(saved_job.history) == 1
            await session.refresh(saved_job.history[0], attribute_names=["products"])
            assert saved_job.history[0].products[0].product_id == "P1"

        # Deleting the job must cascade-delete its history and products.
        async with database.session_scope() as session:
            from sqlalchemy import select

            result = await session.execute(
                select(TrackedJob).where(TrackedJob.id == job_id)
            )
            await session.delete(result.scalar_one())

        async with database.session_scope() as session:
            from sqlalchemy import select

            result = await session.execute(
                select(ScrapeHistory).where(ScrapeHistory.job_id == job_id)
            )
            assert result.scalar_one_or_none() is None


class TestInitDbAndMigrations:
    async def test_init_db_is_idempotent(self):
        await database.init_db()
        await database.init_db()  # second call must not raise

    async def test_init_db_creates_expected_tables(self):
        async with database.engine.connect() as conn:
            result = await conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
            tables = {row[0] for row in result.fetchall()}
        assert {"tracked_jobs", "scrape_history", "scraped_products"} <= tables

    def test_apply_migrations_adds_missing_columns_to_legacy_schema(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            sync_engine = create_engine(f"sqlite:///{tmp_dir}/legacy.db")
            with sync_engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        CREATE TABLE tracked_jobs (
                            id INTEGER PRIMARY KEY,
                            chat_id TEXT NOT NULL,
                            query TEXT NOT NULL,
                            platforms TEXT NOT NULL,
                            schedule_type TEXT NOT NULL,
                            execution_times TEXT NOT NULL,
                            expiration_date TEXT NOT NULL,
                            is_active BOOLEAN NOT NULL DEFAULT 1,
                            created_at TEXT
                        )
                        """
                    )
                )

            with sync_engine.begin() as conn:
                database._apply_migrations(conn)

            with sync_engine.connect() as conn:
                columns = {
                    row[1]
                    for row in conn.execute(
                        text("PRAGMA table_info(tracked_jobs)")
                    ).fetchall()
                }
            assert {"interval_days", "pages_per_run", "include_ads"} <= columns
            sync_engine.dispose()

    def test_apply_migrations_is_a_noop_on_current_schema(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            sync_engine = create_engine(f"sqlite:///{tmp_dir}/current.db")
            with sync_engine.begin() as conn:
                database.Base.metadata.create_all(conn)

            # Columns already present — running migrations again must not
            # raise (exercises the "column already exists, skip" branches).
            with sync_engine.begin() as conn:
                database._apply_migrations(conn)
            sync_engine.dispose()
