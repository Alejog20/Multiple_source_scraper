"""tests/test_diag.py — Unit tests for bot.py's /diag command.

No real network calls, no real Playwright, no real Telegram connection.
/diag reuses scripts/probe_amazon.py's strategy runner, so these tests
patch that module's functions rather than re-implementing fetch logic.
"""

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST-TOKEN")

import bot as bot_module  # noqa: E402


class TestCmdDiag(unittest.IsolatedAsyncioTestCase):
    async def test_non_admin_denied(self):
        update = MagicMock()
        update.effective_user = MagicMock(id=1)
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        with patch.object(bot_module, "ADMIN_USER_ID", 999):
            await bot_module.cmd_diag(update, context)

        update.message.reply_text.assert_called_once_with("Admin only.")

    async def test_admin_schedules_background_report(self):
        update = MagicMock()
        update.effective_user = MagicMock(id=999)
        update.effective_chat.id = 555
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        fake_report = AsyncMock()
        with (
            patch.object(bot_module, "ADMIN_USER_ID", 999),
            patch.object(bot_module, "_run_diag_and_report", fake_report),
        ):
            await bot_module.cmd_diag(update, context)
            await asyncio.sleep(0)  # let the scheduled background task run

        update.message.reply_text.assert_called_once()
        fake_report.assert_called_once_with(context, 555)


class TestRunDiagAndReport(unittest.IsolatedAsyncioTestCase):
    async def test_success_sends_summary_with_status_icons(self):
        context = MagicMock()
        context.bot.send_message = AsyncMock()

        ok_attempt = MagicMock(error=None, block_reason=None, products_found=5)
        blocked_attempt = MagicMock(error=None, block_reason="captcha_form", products_found=0)
        error_attempt = MagicMock(error="TimeoutError: boom", block_reason=None, products_found=0)

        results = {
            "httpx_desktop": ok_attempt,
            "httpx_mobile": blocked_attempt,
            "curl_cffi_chrome": error_attempt,
            "playwright_stealth": ok_attempt,
            "playwright_plain": ok_attempt,
        }

        async def fake_run_attempt(strategy, query, page, parser):
            return results[strategy]

        with (
            patch("scripts.probe_amazon.get_public_ip", AsyncMock(return_value="1.2.3.4")),
            patch("scripts.probe_amazon.run_attempt", fake_run_attempt),
        ):
            await bot_module._run_diag_and_report(context, 777)

        context.bot.send_message.assert_called_once()
        kwargs = context.bot.send_message.call_args.kwargs
        self.assertEqual(kwargs["chat_id"], 777)
        text = kwargs["text"]
        self.assertIn("1.2.3.4", text)
        self.assertIn("✅ httpx_desktop: 5 product(s)", text)
        self.assertIn("⚠️ httpx_mobile: captcha_form", text)
        self.assertIn("❌ curl_cffi_chrome: TimeoutError: boom", text)

    async def test_failure_reports_error_to_chat_instead_of_raising(self):
        context = MagicMock()
        context.bot.send_message = AsyncMock()

        with patch(
            "scripts.probe_amazon.get_public_ip", AsyncMock(side_effect=RuntimeError("boom"))
        ):
            await bot_module._run_diag_and_report(context, 777)  # must not raise

        context.bot.send_message.assert_called_once()
        self.assertIn("Diagnostics failed", context.bot.send_message.call_args.kwargs["text"])

    async def test_send_failure_after_probe_failure_is_only_logged(self):
        context = MagicMock()
        context.bot.send_message = AsyncMock(side_effect=RuntimeError("network down"))

        with patch(
            "scripts.probe_amazon.get_public_ip", AsyncMock(side_effect=RuntimeError("boom"))
        ):
            await bot_module._run_diag_and_report(context, 777)  # must not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)
