"""tests/test_bot_security.py — Unit tests for bot.py access control & input limits.

No real network calls, no real Playwright, no real Telegram connection.
bot.py is not in the coverage gate (it's interface glue, per CLAUDE.md's
layering rule) but still needs tests — this file covers the fail-closed
allowlist and the input-limit guards added for the security hardening pass.
"""

import logging
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:TOKEN")

import bot as bot_module  # noqa: E402
from telegram.ext import ApplicationHandlerStop  # noqa: E402


def _make_update(user_id: int | None):
    update = MagicMock()
    if user_id is None:
        update.effective_user = None
    else:
        update.effective_user = MagicMock(id=user_id)
    return update


class TestAllowlistGatekeeper(unittest.IsolatedAsyncioTestCase):
    """Covers the TypeHandler(Update, ...) registered at group=-1."""

    async def test_unauthorized_user_is_rejected_and_logged(self):
        update = _make_update(999)
        with (
            patch.object(bot_module, "ALLOWED_USER_IDS", frozenset({123})),
            patch.object(bot_module, "ADMIN_USER_ID", 456),
            self.assertLogs(bot_module._logger, level="WARNING") as log_ctx,
        ):
            with self.assertRaises(ApplicationHandlerStop):
                await bot_module._reject_unauthorized(update, MagicMock())
        self.assertIn("999", log_ctx.output[0])

    async def test_allowlisted_user_passes_through(self):
        update = _make_update(123)
        with (
            patch.object(bot_module, "ALLOWED_USER_IDS", frozenset({123})),
            patch.object(bot_module, "ADMIN_USER_ID", 456),
        ):
            result = await bot_module._reject_unauthorized(update, MagicMock())
        self.assertIsNone(result)

    async def test_admin_passes_even_if_not_in_allowlist(self):
        update = _make_update(456)
        with (
            patch.object(bot_module, "ALLOWED_USER_IDS", frozenset({123})),
            patch.object(bot_module, "ADMIN_USER_ID", 456),
        ):
            result = await bot_module._reject_unauthorized(update, MagicMock())
        self.assertIsNone(result)

    async def test_empty_allowlist_and_no_admin_rejects_everyone(self):
        update = _make_update(123456789)
        with (
            patch.object(bot_module, "ALLOWED_USER_IDS", frozenset()),
            patch.object(bot_module, "ADMIN_USER_ID", None),
        ):
            with self.assertRaises(ApplicationHandlerStop):
                await bot_module._reject_unauthorized(update, MagicMock())

    async def test_update_with_no_effective_user_is_rejected(self):
        update = _make_update(None)
        with (
            patch.object(bot_module, "ALLOWED_USER_IDS", frozenset({123})),
            patch.object(bot_module, "ADMIN_USER_ID", 456),
        ):
            with self.assertRaises(ApplicationHandlerStop):
                await bot_module._reject_unauthorized(update, MagicMock())

    def test_gatekeeper_registered_as_group_minus_one(self):
        """build_application() must wire the allowlist handler ahead of
        every command/conversation handler so it can short-circuit them."""
        with patch.object(bot_module, "ADMIN_USER_ID", 1):
            app = bot_module.build_application()
        self.assertIn(-1, app.handlers)
        handler_types = [type(h).__name__ for h in app.handlers[-1]]
        self.assertIn("TypeHandler", handler_types)


class TestIsAdmin(unittest.TestCase):
    def test_is_admin_true_for_matching_id(self):
        with patch.object(bot_module, "ADMIN_USER_ID", 42):
            self.assertTrue(bot_module._is_admin(42))

    def test_is_admin_false_for_other_id(self):
        with patch.object(bot_module, "ADMIN_USER_ID", 42):
            self.assertFalse(bot_module._is_admin(7))

    def test_is_admin_false_when_unset(self):
        with patch.object(bot_module, "ADMIN_USER_ID", None):
            self.assertFalse(bot_module._is_admin(42))

    def test_is_admin_false_for_none_user_id(self):
        with patch.object(bot_module, "ADMIN_USER_ID", 42):
            self.assertFalse(bot_module._is_admin(None))


class TestNoisyLoggersSilenced(unittest.TestCase):
    """httpx/httpcore log every request URL at INFO — which includes the
    bot token in the Telegram API path. They must stay at WARNING or above
    so the token never lands in logs."""

    def test_httpx_and_httpcore_silenced_to_warning(self):
        for name in ("httpx", "httpcore", "telegram", "apscheduler"):
            self.assertGreaterEqual(logging.getLogger(name).level, logging.WARNING)


class TestTrackStartActiveJobLimit(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_new_job_when_at_limit(self):
        import contextlib

        existing_jobs = [MagicMock() for _ in range(bot_module.MAX_ACTIVE_JOBS_PER_USER)]
        scalar_mock = MagicMock()
        scalar_mock.scalars.return_value.all.return_value = existing_jobs
        session_mock = AsyncMock()
        session_mock.execute = AsyncMock(return_value=scalar_mock)

        @contextlib.asynccontextmanager
        async def fake_session_scope():
            yield session_mock

        update = MagicMock()
        update.effective_chat.id = 555
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.user_data = {}

        with patch.object(bot_module, "session_scope", fake_session_scope):
            from telegram.ext import ConversationHandler
            result = await bot_module.track_start(update, context)

        self.assertEqual(result, ConversationHandler.END)
        update.message.reply_text.assert_called_once()
        self.assertIn("maximum", update.message.reply_text.call_args[0][0])

    async def test_allows_new_job_below_limit(self):
        import contextlib

        scalar_mock = MagicMock()
        scalar_mock.scalars.return_value.all.return_value = []
        session_mock = AsyncMock()
        session_mock.execute = AsyncMock(return_value=scalar_mock)

        @contextlib.asynccontextmanager
        async def fake_session_scope():
            yield session_mock

        update = MagicMock()
        update.effective_chat.id = 555
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.user_data = {}

        with patch.object(bot_module, "session_scope", fake_session_scope):
            from telegram.ext import ConversationHandler
            result = await bot_module.track_start(update, context)

        self.assertEqual(result, bot_module.PLATFORM)
        self.assertNotEqual(result, ConversationHandler.END)


class TestQueryLengthLimit(unittest.IsolatedAsyncioTestCase):
    async def test_too_short_query_rejected(self):
        update = MagicMock()
        update.message.text = "a"
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.user_data = {}

        result = await bot_module.query_received(update, context)

        self.assertEqual(result, bot_module.QUERY)
        self.assertNotIn("query", context.user_data)

    async def test_too_long_query_rejected(self):
        update = MagicMock()
        update.message.text = "x" * (bot_module.MAX_QUERY_LENGTH + 1)
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.user_data = {}

        result = await bot_module.query_received(update, context)

        self.assertEqual(result, bot_module.QUERY)

    async def test_valid_query_accepted(self):
        update = MagicMock()
        update.message.text = "laptop gaming"
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.user_data = {}

        result = await bot_module.query_received(update, context)

        self.assertEqual(result, bot_module.FREQUENCY)
        self.assertEqual(context.user_data["query"], "laptop gaming")


class TestTimesPerJobLimit(unittest.IsolatedAsyncioTestCase):
    async def test_too_many_times_rejected(self):
        update = MagicMock()
        update.message.text = ",".join(
            f"{h:02d}:00" for h in range(bot_module.MAX_TIMES_PER_JOB + 1)
        )
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.user_data = {}

        result = await bot_module.times_received(update, context)

        self.assertEqual(result, bot_module.TIMES)
        self.assertNotIn("execution_times", context.user_data)

    async def test_times_within_limit_accepted(self):
        update = MagicMock()
        update.message.text = "08:00,20:00"
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.user_data = {}

        result = await bot_module.times_received(update, context)

        self.assertEqual(result, bot_module.PAGES)
        self.assertEqual(context.user_data["execution_times"], ["08:00", "20:00"])


class TestPagesZeroAdminOnly(unittest.IsolatedAsyncioTestCase):
    async def test_non_admin_requesting_all_pages_rejected(self):
        update = MagicMock()
        update.message.text = "0"
        update.message.reply_text = AsyncMock()
        update.effective_user = MagicMock(id=999)
        context = MagicMock()
        context.user_data = {}

        with patch.object(bot_module, "ADMIN_USER_ID", 1):
            result = await bot_module.pages_chosen(update, context)

        self.assertEqual(result, bot_module.PAGES)
        self.assertNotIn("pages_per_run", context.user_data)

    async def test_admin_requesting_all_pages_accepted(self):
        update = MagicMock()
        update.message.text = "0"
        update.message.reply_text = AsyncMock()
        update.effective_user = MagicMock(id=1)
        context = MagicMock()
        context.user_data = {}

        with patch.object(bot_module, "ADMIN_USER_ID", 1):
            result = await bot_module.pages_chosen(update, context)

        self.assertEqual(result, bot_module.ADS)
        self.assertEqual(context.user_data["pages_per_run"], 0)

    async def test_non_admin_requesting_limited_pages_accepted(self):
        update = MagicMock()
        update.message.text = "3"
        update.message.reply_text = AsyncMock()
        update.effective_user = MagicMock(id=999)
        context = MagicMock()
        context.user_data = {}

        with patch.object(bot_module, "ADMIN_USER_ID", 1):
            result = await bot_module.pages_chosen(update, context)

        self.assertEqual(result, bot_module.ADS)
        self.assertEqual(context.user_data["pages_per_run"], 3)


class TestDurationLimit(unittest.IsolatedAsyncioTestCase):
    async def test_duration_above_configured_max_rejected(self):
        update = MagicMock()
        update.message.text = str(bot_module.MAX_JOB_DURATION_DAYS + 1)
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.user_data = {"query": "q", "platforms": ["amazon"],
                              "schedule_type": "daily",
                              "execution_times": ["08:00"]}

        result = await bot_module.duration_received(update, context)

        self.assertEqual(result, bot_module.DURATION)


if __name__ == "__main__":
    unittest.main(verbosity=2)
