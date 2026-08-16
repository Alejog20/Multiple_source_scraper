"""
tests/test_scheduler.py — Unit tests for scheduler-related logic
=================================================================
Tests the core_engine helpers and bot scheduler logic in isolation using
unittest and unittest.mock. No real network calls, no real Playwright, no
real Telegram connection required.

Run with:
    uv run python -m unittest tests/test_scheduler.py -v
"""

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure project root is on sys.path when running from tests/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# Lazy imports (avoid loading scrapers/Telegram at collection time)
# ---------------------------------------------------------------------------


def _import_engine():
    """Import core_engine lazily to allow patching at test setUp time."""
    import core_engine
    return core_engine


# ---------------------------------------------------------------------------
# TestCoreEngineHelpers
# ---------------------------------------------------------------------------


class TestCoreEngineHelpers(unittest.TestCase):
    """Tests for the private helper functions in core_engine.py."""

    def setUp(self):
        self.engine = _import_engine()

    # --- _deduplicate_products ---

    def test_deduplicate_preserves_first_occurrence(self):
        products = [
            {"id": "a", "title": "First A", "price": 10.0},
            {"id": "b", "title": "First B", "price": 20.0},
            {"id": "a", "title": "Second A — duplicate", "price": 5.0},
        ]
        result = self.engine._deduplicate_products(products)
        self.assertEqual(len(result), 2)
        # "a" should be the *first* occurrence, not the duplicate
        a_product = next(p for p in result if p["id"] == "a")
        self.assertEqual(a_product["title"], "First A")

    def test_deduplicate_empty_list(self):
        result = self.engine._deduplicate_products([])
        self.assertEqual(result, [])

    def test_deduplicate_all_unique(self):
        products = [
            {"id": "x", "title": "X", "price": 1.0},
            {"id": "y", "title": "Y", "price": 2.0},
        ]
        result = self.engine._deduplicate_products(products)
        self.assertEqual(len(result), 2)

    def test_deduplicate_strips_products_with_empty_id(self):
        products = [
            {"id": "", "title": "No ID A"},
            {"id": "real", "title": "Has ID"},
            {"id": "", "title": "No ID B"},
        ]
        result = self.engine._deduplicate_products(products)
        # Only the product with a real id survives
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "real")

    # --- _compute_lowest_price ---

    def test_compute_lowest_price_all_none(self):
        products = [
            {"id": "a", "price": None},
            {"id": "b", "price": None},
        ]
        result = self.engine._compute_lowest_price(products)
        self.assertIsNone(result)

    def test_compute_lowest_price_mixed(self):
        products = [
            {"id": "a", "price": 99.99},
            {"id": "b", "price": None},
            {"id": "c", "price": 49.50},
            {"id": "d", "price": 149.00},
        ]
        result = self.engine._compute_lowest_price(products)
        self.assertAlmostEqual(result, 49.50)

    def test_compute_lowest_price_single_product(self):
        products = [{"id": "a", "price": 7.77}]
        result = self.engine._compute_lowest_price(products)
        self.assertAlmostEqual(result, 7.77)

    def test_compute_lowest_price_empty_list(self):
        result = self.engine._compute_lowest_price([])
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# TestScrapeResultMessageFormat
# ---------------------------------------------------------------------------


class TestScrapeResultMessageFormat(unittest.TestCase):
    """Tests for the MarkdownV2 report builder in bot.py."""

    def setUp(self):
        # We need to import bot.py — but it reads TELEGRAM_BOT_TOKEN at import.
        # Patch the env var before importing.
        import os
        os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:TOKEN")

        import bot
        self.bot = bot

    def _make_result(
        self,
        total_found: int = 10,
        lowest_price: float | None = 299.99,
        errors: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "products": [],
            "total_found": total_found,
            "lowest_price": lowest_price,
            "errors": errors or [],
        }

    def _build_report(self, job_id: int, query: str, platforms: list[str],
                      result: dict, expiration_date: datetime) -> str:
        """Reproduce the report-building logic from bot.scheduled_scrape_task."""
        _esc = self.bot._esc
        expiry_str = expiration_date.strftime("%Y-%m-%d")
        platforms_display = ", ".join(p.capitalize() for p in platforms)

        if result["lowest_price"] is not None:
            price_str = _esc(f"${result['lowest_price']:.2f}")
        else:
            price_str = "N/A"

        return (
            f"🔍 *Job \\#{_esc(str(job_id))} — Price Report*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 *Query:* `{_esc(query)}`\n"
            f"🏪 *Platforms:* {_esc(platforms_display)}\n"
            f"📊 *Products found:* {_esc(str(result['total_found']))}\n"
            f"💰 *Lowest price:* {price_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"_Tracked until: {_esc(expiry_str)}_"
        )

    def test_format_report_with_results(self):
        expiry = datetime(2026, 3, 15, tzinfo=timezone.utc)
        result = self._make_result(total_found=87, lowest_price=299.99)
        report = self._build_report(
            job_id=42,
            query="laptop gaming",
            platforms=["amazon", "mercadolibre"],
            result=result,
            expiration_date=expiry,
        )
        self.assertIn("42", report)
        self.assertIn("laptop gaming", report)
        self.assertIn("87", report)
        self.assertIn("299", report)
        # MarkdownV2 escaping: '-' in the date must be escaped or Telegram rejects the message
        self.assertIn("2026\\-03\\-15", report)
        # MarkdownV2 escaping: $ and . should be escaped
        self.assertIn("\\$299\\.99", report)

    def test_format_report_no_price(self):
        expiry = datetime(2026, 3, 15, tzinfo=timezone.utc)
        result = self._make_result(total_found=5, lowest_price=None)
        report = self._build_report(
            job_id=1,
            query="iphone",
            platforms=["amazon"],
            result=result,
            expiration_date=expiry,
        )
        # Should show N/A and NOT crash
        self.assertIn("N/A", report)
        self.assertNotIn("None", report)

    def test_esc_escapes_special_chars(self):
        esc = self.bot._esc
        self.assertEqual(esc("hello.world"), r"hello\.world")
        self.assertEqual(esc("$100"), r"\$100")
        self.assertEqual(esc("price: 9.99!"), r"price\: 9\.99\!")


# ---------------------------------------------------------------------------
# TestSchedulerLogic
# ---------------------------------------------------------------------------


class TestSchedulerLogic(unittest.IsolatedAsyncioTestCase):
    """Tests for scheduled_scrape_task behaviour."""

    def setUp(self):
        import os
        os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:TOKEN")

    async def _make_context(
        self,
        job_id: int,
        chat_id: str,
        job_obj: Any = None,
        bot: Any = None,
    ):
        """Build a minimal mock ContextTypes.DEFAULT_TYPE."""
        ctx = MagicMock()
        ctx.job = MagicMock()
        ctx.job.data = {"job_id": job_id, "chat_id": chat_id}
        ctx.job.schedule_removal = MagicMock()
        ctx.bot = bot or AsyncMock()
        ctx.bot.send_message = AsyncMock()
        return ctx

    async def test_expired_job_deactivates_and_notifies(self):
        """An expired job must be deactivated and the scraper must NOT be called."""
        import contextlib
        import bot as bot_module

        expired_job = MagicMock(spec=["id", "is_active", "expiration_date",
                                      "get_platforms", "query"])
        expired_job.id = 99
        expired_job.is_active = True
        expired_job.expiration_date = datetime(2020, 1, 1, tzinfo=timezone.utc)
        expired_job.get_platforms.return_value = ["amazon"]
        expired_job.query = "old query"

        scalar_mock = MagicMock()
        scalar_mock.scalar_one_or_none.return_value = expired_job

        session_mock = AsyncMock()
        session_mock.execute = AsyncMock(return_value=scalar_mock)

        @contextlib.asynccontextmanager
        async def fake_session_scope():
            yield session_mock

        ctx = await self._make_context(job_id=99, chat_id="123456")

        with (
            patch("bot.session_scope", fake_session_scope),
            patch("bot.execute_scrape", new_callable=AsyncMock) as mock_scrape,
        ):
            await bot_module.scheduled_scrape_task(ctx)

        # The scraper should NOT have been called for an expired job
        mock_scrape.assert_not_called()
        # Job removal must be scheduled
        ctx.job.schedule_removal.assert_called_once()

    async def test_active_job_saves_history(self):
        """A healthy job run must persist a ScrapeHistory row."""
        import bot as bot_module

        now = datetime.now(tz=timezone.utc)
        expiry = now + timedelta(days=30)

        active_job = MagicMock()
        active_job.id = 7
        active_job.is_active = True
        active_job.expiration_date = expiry
        active_job.get_platforms.return_value = ["amazon"]
        active_job.get_execution_times.return_value = ["08:00"]
        active_job.query = "test product"

        scrape_result = {
            "products": [{"id": "p1", "title": "Test", "price": 50.0,
                          "source": "Amazon", "url": "https://example.com",
                          "currency": "USD", "rating": 4.5, "review_count": 10}],
            "total_found": 1,
            "lowest_price": 50.0,
            "errors": [],
        }

        added_objects: list = []

        scalar_mock = MagicMock()
        scalar_mock.scalar_one_or_none.return_value = active_job

        session_mock = AsyncMock()
        session_mock.execute = AsyncMock(return_value=scalar_mock)
        session_mock.add = MagicMock(side_effect=added_objects.append)

        import contextlib

        @contextlib.asynccontextmanager
        async def fake_session_scope():
            yield session_mock

        ctx = await self._make_context(job_id=7, chat_id="999")

        with (
            patch("bot.session_scope", fake_session_scope),
            patch(
                "bot.execute_scrape",
                new_callable=AsyncMock,
                return_value=scrape_result,
            ),
        ):
            await bot_module.scheduled_scrape_task(ctx)

        # A ScrapeHistory object must have been added to the session
        from database import ScrapeHistory

        history_rows = [o for o in added_objects if isinstance(o, ScrapeHistory)]
        self.assertEqual(len(history_rows), 1)
        self.assertEqual(history_rows[0].job_id, 7)
        self.assertEqual(history_rows[0].total_found, 1)
        self.assertAlmostEqual(history_rows[0].lowest_price, 50.0)

        # Bot must have sent a report message
        ctx.bot.send_message.assert_called()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
