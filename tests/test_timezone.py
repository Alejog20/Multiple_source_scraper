"""tests/test_timezone.py — Unit tests for BOT_TIMEZONE scheduling.

Verifies that _register_job_in_scheduler builds JobQueue entries that fire
at the correct UTC instant for a job's local wall-clock time, for all three
schedule types (daily/weekly/custom_days), and that legacy jobs stamped
with timezone="UTC" (the migration default) keep their original behaviour.

No real network calls, no real Playwright, no real Telegram connection —
the trigger's next-fire-time is computed directly rather than starting the
live AsyncIOScheduler, which needs a running event loop lifecycle this test
doesn't otherwise require.
"""

import os
import sys
import unittest
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST-TOKEN")

import bot as bot_module  # noqa: E402


def _make_job(job_id: int, schedule_type: str, tz: str, times=("08:00",), interval_days=None):
    job = MagicMock()
    job.id = job_id
    job.chat_id = "123"
    job.pages_per_run = 2
    job.include_ads = True
    job.get_execution_times.return_value = list(times)
    job.schedule_type = schedule_type
    job.timezone = tz
    job.interval_days = interval_days
    return job


class TestTimezoneAwareScheduling(unittest.TestCase):
    def setUp(self):
        self.app = bot_module.build_application()
        self.now = datetime.now(dt_timezone.utc)

    def _next_fire(self, job_name: str):
        aps_job = self.app.job_queue.get_jobs_by_name(job_name)[0].job
        return aps_job.trigger.get_next_fire_time(None, self.now)

    def test_daily_job_08_00_bogota_fires_at_13_00_utc(self):
        job = _make_job(101, "daily", "America/Bogota", times=("08:00",))
        bot_module._register_job_in_scheduler(self.app, job)

        fire_time = self._next_fire("job_101")

        self.assertEqual(fire_time.astimezone(dt_timezone.utc).hour, 13)
        local = fire_time.astimezone(ZoneInfo("America/Bogota"))
        self.assertEqual(local.hour, 8)
        self.assertEqual(local.minute, 0)

    def test_legacy_utc_job_keeps_firing_at_08_00_utc(self):
        """A job created before the timezone column existed (default 'UTC')
        must keep the exact same schedule it always had."""
        job = _make_job(102, "daily", "UTC", times=("08:00",))
        bot_module._register_job_in_scheduler(self.app, job)

        fire_time = self._next_fire("job_102")

        self.assertEqual(fire_time.utcoffset().total_seconds(), 0)
        self.assertEqual(fire_time.hour, 8)

    def test_weekly_job_first_run_lands_on_correct_local_hour(self):
        job = _make_job(103, "weekly", "America/Bogota", times=("20:30",))
        bot_module._register_job_in_scheduler(self.app, job)

        fire_time = self._next_fire("job_103")
        local = fire_time.astimezone(ZoneInfo("America/Bogota"))

        self.assertEqual(local.hour, 20)
        self.assertEqual(local.minute, 30)

    def test_custom_days_job_first_run_lands_on_correct_local_hour(self):
        job = _make_job(
            104, "custom_days", "America/Bogota", times=("08:00",), interval_days=3
        )
        bot_module._register_job_in_scheduler(self.app, job)

        fire_time = self._next_fire("job_104")
        local = fire_time.astimezone(ZoneInfo("America/Bogota"))

        self.assertEqual(local.hour, 8)
        self.assertEqual(fire_time.astimezone(dt_timezone.utc).hour, 13)

    def test_multiple_execution_times_each_register_their_own_trigger_tz(self):
        job = _make_job(105, "daily", "America/Bogota", times=("06:00", "18:00"))
        bot_module._register_job_in_scheduler(self.app, job)

        jobs = self.app.job_queue.get_jobs_by_name("job_105")
        self.assertEqual(len(jobs), 2)
        for jq_job in jobs:
            fire_time = jq_job.job.trigger.get_next_fire_time(None, self.now)
            local = fire_time.astimezone(ZoneInfo("America/Bogota"))
            self.assertIn(local.hour, (6, 18))


class TestNewJobStampedWithBotTimezone(unittest.IsolatedAsyncioTestCase):
    """duration_received() is where a TrackedJob row is actually built —
    it must stamp new jobs with BOT_TIMEZONE, never a hardcoded 'UTC'."""

    async def test_duration_received_creates_job_with_configured_timezone(self):
        import contextlib
        from unittest.mock import AsyncMock, patch

        from database import TrackedJob

        added_objects: list = []
        session_mock = AsyncMock()
        session_mock.add = MagicMock(side_effect=added_objects.append)

        saved_job = MagicMock()

        scalar_mock = MagicMock()
        scalar_mock.scalar_one.return_value = saved_job

        call_count = {"n": 0}

        async def fake_execute(*args, **kwargs):
            call_count["n"] += 1
            return scalar_mock

        session_mock.execute = fake_execute

        @contextlib.asynccontextmanager
        async def fake_session_scope():
            yield session_mock

        update = MagicMock()
        update.message.text = "7"
        update.message.reply_text = AsyncMock()
        update.effective_chat.id = 555

        context = MagicMock()
        context.user_data = {
            "query": "laptop",
            "platforms": ["amazon"],
            "schedule_type": "daily",
            "execution_times": ["08:00"],
            "pages_per_run": 2,
            "include_ads": True,
        }

        with (
            patch.object(bot_module, "session_scope", fake_session_scope),
            patch.object(bot_module, "_register_job_in_scheduler"),
            patch.object(bot_module, "BOT_TIMEZONE", "America/Bogota"),
        ):
            await bot_module.duration_received(update, context)

        jobs = [o for o in added_objects if isinstance(o, TrackedJob)]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].timezone, "America/Bogota")

        sent_text = update.message.reply_text.call_args[0][0]
        self.assertIn("America/Bogota", sent_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
