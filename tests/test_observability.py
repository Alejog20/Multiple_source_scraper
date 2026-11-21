"""tests/test_observability.py — Unit tests for bot.py's error handler,
/runnow, /health, and the heartbeat task.

No real network calls, no real Playwright, no real Telegram connection.
bot.py is interface glue (not in the coverage gate) but these are the
behaviours the Fase 3 acceptance criteria call out explicitly.
"""

import contextlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST-TOKEN")

import bot as bot_module  # noqa: E402


def _session_scope_yielding(session_mock):
    @contextlib.asynccontextmanager
    async def fake_session_scope():
        yield session_mock

    return fake_session_scope


class TestErrorHandler(unittest.IsolatedAsyncioTestCase):
    async def test_no_admin_configured_does_not_send_anything(self):
        context = MagicMock()
        context.error = ValueError("boom")
        context.job = None
        context.bot.send_message = AsyncMock()

        with patch.object(bot_module, "ADMIN_USER_ID", None):
            await bot_module.error_handler(MagicMock(), context)

        context.bot.send_message.assert_not_called()

    async def test_admin_receives_truncated_summary_with_job_id(self):
        context = MagicMock()
        context.error = RuntimeError("scrape exploded")
        context.job = MagicMock()
        context.job.data = {"job_id": 42}
        context.bot.send_message = AsyncMock()

        with patch.object(bot_module, "ADMIN_USER_ID", 999):
            await bot_module.error_handler(MagicMock(), context)

        context.bot.send_message.assert_called_once()
        kwargs = context.bot.send_message.call_args.kwargs
        self.assertEqual(kwargs["chat_id"], 999)
        self.assertIn("RuntimeError", kwargs["text"])
        self.assertIn("job #42", kwargs["text"])
        self.assertLessEqual(len(kwargs["text"]), 1000)

    async def test_send_failure_is_logged_not_raised(self):
        context = MagicMock()
        context.error = ValueError("boom")
        context.job = None
        context.bot.send_message = AsyncMock(side_effect=RuntimeError("network down"))

        with patch.object(bot_module, "ADMIN_USER_ID", 999):
            await bot_module.error_handler(MagicMock(), context)  # must not raise


class TestRunNow(unittest.IsolatedAsyncioTestCase):
    async def test_missing_args_shows_usage(self):
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = []

        await bot_module.cmd_runnow(update, context)

        update.message.reply_text.assert_called_once_with("Usage: /runnow <job_id>")

    async def test_non_numeric_arg_shows_usage(self):
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = ["abc"]

        await bot_module.cmd_runnow(update, context)

        update.message.reply_text.assert_called_once_with("Usage: /runnow <job_id>")

    async def test_job_not_owned_is_denied(self):
        update = MagicMock()
        update.effective_chat.id = 555
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = ["7"]

        other_users_job = MagicMock()
        other_users_job.chat_id = "999"
        scalar_mock = MagicMock()
        scalar_mock.scalar_one_or_none.return_value = other_users_job
        session_mock = AsyncMock()
        session_mock.execute = AsyncMock(return_value=scalar_mock)

        with patch.object(bot_module, "session_scope", _session_scope_yielding(session_mock)):
            await bot_module.cmd_runnow(update, context)

        update.message.reply_text.assert_called_once_with("Job not found or access denied.")

    async def test_owned_job_is_run_immediately(self):
        update = MagicMock()
        update.effective_chat.id = 555
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = ["7"]
        context.job_queue.run_once = MagicMock()

        job = MagicMock()
        job.id = 7
        job.chat_id = "555"
        job.pages_per_run = 2
        job.include_ads = True
        scalar_mock = MagicMock()
        scalar_mock.scalar_one_or_none.return_value = job
        session_mock = AsyncMock()
        session_mock.execute = AsyncMock(return_value=scalar_mock)

        with patch.object(bot_module, "session_scope", _session_scope_yielding(session_mock)):
            await bot_module.cmd_runnow(update, context)

        context.job_queue.run_once.assert_called_once()
        call_kwargs = context.job_queue.run_once.call_args.kwargs
        self.assertEqual(call_kwargs["data"]["job_id"], 7)
        update.message.reply_text.assert_called_once_with("Running job #7 now…")


class TestHealth(unittest.IsolatedAsyncioTestCase):
    async def test_non_admin_denied(self):
        update = MagicMock()
        update.effective_user = MagicMock(id=123)
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        with patch.object(bot_module, "ADMIN_USER_ID", 999):
            await bot_module.cmd_health(update, context)

        update.message.reply_text.assert_called_once_with("Admin only.")

    async def test_admin_gets_status_snapshot(self):
        update = MagicMock()
        update.effective_user = MagicMock(id=999)
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        scalar_mock = MagicMock()
        scalar_mock.scalar_one.side_effect = [3, "2026-01-01 00:00:00"]
        session_mock = AsyncMock()
        session_mock.execute = AsyncMock(return_value=scalar_mock)

        fake_response = MagicMock()
        fake_response.text = "1.2.3.4\n"
        fake_client = AsyncMock()
        fake_client.get = AsyncMock(return_value=fake_response)
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.object(bot_module, "ADMIN_USER_ID", 999),
            patch.object(bot_module, "session_scope", _session_scope_yielding(session_mock)),
            patch("httpx.AsyncClient", return_value=fake_client),
        ):
            await bot_module.cmd_health(update, context)

        update.message.reply_text.assert_called_once()
        sent_text = update.message.reply_text.call_args[0][0]
        self.assertIn("Active jobs: 3", sent_text)
        self.assertIn("1\\.2\\.3\\.4", sent_text)

    async def test_admin_health_survives_ip_lookup_failure(self):
        update = MagicMock()
        update.effective_user = MagicMock(id=999)
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        scalar_mock = MagicMock()
        scalar_mock.scalar_one.side_effect = [0, None]
        session_mock = AsyncMock()
        session_mock.execute = AsyncMock(return_value=scalar_mock)

        with (
            patch.object(bot_module, "ADMIN_USER_ID", 999),
            patch.object(bot_module, "session_scope", _session_scope_yielding(session_mock)),
            patch("httpx.AsyncClient", side_effect=RuntimeError("no network")),
        ):
            await bot_module.cmd_health(update, context)

        sent_text = update.message.reply_text.call_args[0][0]
        self.assertIn("unknown", sent_text)
        self.assertIn("never", sent_text)


class TestHeartbeat(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_gets_configured_url(self):
        fake_client = AsyncMock()
        fake_client.get = AsyncMock()
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.object(bot_module, "HEARTBEAT_URL", "https://hc-ping.com/abc"),
            patch("httpx.AsyncClient", return_value=fake_client),
        ):
            await bot_module._heartbeat_task(MagicMock())

        fake_client.get.assert_called_once_with("https://hc-ping.com/abc")

    async def test_heartbeat_failure_does_not_raise(self):
        with (
            patch.object(bot_module, "HEARTBEAT_URL", "https://hc-ping.com/abc"),
            patch("httpx.AsyncClient", side_effect=RuntimeError("down")),
        ):
            await bot_module._heartbeat_task(MagicMock())  # must not raise


class TestHealthFileTouch(unittest.IsolatedAsyncioTestCase):
    async def test_touches_configured_health_file(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp_dir:
            health_file = str(Path(tmp_dir) / "healthy")
            with patch.object(bot_module, "HEALTH_FILE", health_file):
                await bot_module._touch_health_file(MagicMock())
            self.assertTrue(Path(health_file).exists())

    async def test_touch_failure_is_logged_not_raised(self):
        with patch.object(bot_module, "HEALTH_FILE", "/nonexistent-dir/healthy"):
            await bot_module._touch_health_file(MagicMock())  # must not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)
