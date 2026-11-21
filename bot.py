"""
bot.py — Telegram Bot Entry Point
===================================
Daemonized Telegram bot that exposes price-tracking commands to users and
drives the JobQueue scheduler. All business logic lives in core_engine.py;
all persistence lives in database.py. This module owns only:

  - Command handlers and ConversationHandler state machine (/track)
  - Formatting of Telegram messages (MarkdownV2)
  - JobQueue registration and recovery
  - Application startup / shutdown lifecycle

Environment variables (loaded from .env):
  TELEGRAM_BOT_TOKEN       — required, obtained from BotFather
  ALLOWED_USER_IDS         — required, comma-separated Telegram user IDs; empty = reject everyone
  ADMIN_USER_ID            — required for admin features (/health, /diag, error alerts)
  PAGES_PER_SCRAPE         — optional, default 2 (pages passed to execute_scrape)
  MIN_QUERY_LENGTH         — optional, default 2
  MAX_QUERY_LENGTH         — optional, default 100
  MAX_TIMES_PER_JOB        — optional, default 6
  MAX_ACTIVE_JOBS_PER_USER — optional, default 10
  MAX_JOB_DURATION_DAYS    — optional, default 90
"""

import asyncio
import html
import io
import logging
import os
import re
import ssl
import warnings
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# SSL: inject Windows system certificate store into Python's ssl module.
# Required on Windows (and corporate networks on any OS) where the system
# trust store contains internal/proxy CA certificates that Python's bundled
# OpenSSL does not know about. Must run before any SSL connections are made.
# ---------------------------------------------------------------------------
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass  # truststore not installed; fall back to default SSL behaviour

import httpx
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.text import Text
from sqlalchemy import func, select
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from core_engine import execute_scrape
from database import ScrapedProduct, ScrapeHistory, TrackedJob, init_db, session_scope

# ---------------------------------------------------------------------------
# Bootstrap — logging first, everything else after
# ---------------------------------------------------------------------------

load_dotenv()

_console = Console()


def _setup_logging() -> None:
    """Replace the default file-only logger with Rich console + file output.

    Removes any handlers already attached by core_engine's import-time call
    to setup_logging(), then installs:
      - RichHandler   → coloured, structured output in the terminal
      - FileHandler   → plain-text append log for persistence (bot.log)

    Noisy libraries (httpx, telegram internals, apscheduler) are silenced to
    WARNING so the console isn't flooded with HTTP traffic details.
    """
    root = logging.getLogger()
    # Clear handlers set by earlier setup_logging() calls (e.g. from
    # core_engine import) so we don't get duplicate lines.
    for h in root.handlers[:]:
        root.removeHandler(h)
        h.close()

    rich_handler = RichHandler(
        console=_console,
        rich_tracebacks=True,
        tracebacks_show_locals=False,
        show_path=False,
        markup=True,
        log_time_format="[%H:%M:%S]",
    )
    rich_handler.setLevel(logging.INFO)

    file_handler = logging.FileHandler("bot.log", mode="a", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        datefmt="[%H:%M:%S]",
        handlers=[rich_handler, file_handler],
        force=True,  # override any previously-set root handlers
    )

    for noisy in ("httpx", "httpcore", "telegram", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_setup_logging()
_logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
PAGES_PER_SCRAPE: int = int(os.getenv("PAGES_PER_SCRAPE", "2"))

# ---------------------------------------------------------------------------
# Access control — fail-closed allowlist
# ---------------------------------------------------------------------------
# ALLOWED_USER_IDS is the only way in. If it's empty (the out-of-the-box
# state), every update is rejected and logged at WARNING with the sender's
# user_id, so the operator can grab their own ID from the log on first
# contact and add it to the env var. ADMIN_USER_ID is always implicitly
# allowed even if omitted from ALLOWED_USER_IDS, since admin-only commands
# (/health, /diag) re-check it independently.

ALLOWED_USER_IDS: frozenset[int] = frozenset(
    int(uid) for uid in os.getenv("ALLOWED_USER_IDS", "").split(",") if uid.strip()
)
ADMIN_USER_ID: int | None = (
    int(os.environ["ADMIN_USER_ID"]) if os.getenv("ADMIN_USER_ID") else None
)

# ---------------------------------------------------------------------------
# Input limits — configurable via env, safe defaults
# ---------------------------------------------------------------------------

MIN_QUERY_LENGTH: int = int(os.getenv("MIN_QUERY_LENGTH", "2"))
MAX_QUERY_LENGTH: int = int(os.getenv("MAX_QUERY_LENGTH", "100"))
MAX_TIMES_PER_JOB: int = int(os.getenv("MAX_TIMES_PER_JOB", "6"))
"""Cap on the number of HH:MM run-slots a single job may register. Without
this, a user could request e.g. 24 daily run times, turning one tracking
job into an hourly-hammering job against Amazon/MercadoLibre."""
MAX_ACTIVE_JOBS_PER_USER: int = int(os.getenv("MAX_ACTIVE_JOBS_PER_USER", "10"))
MAX_JOB_DURATION_DAYS: int = int(os.getenv("MAX_JOB_DURATION_DAYS", "90"))

# ---------------------------------------------------------------------------
# Timezone — new jobs are scheduled in BOT_TIMEZONE wall-clock time
# ---------------------------------------------------------------------------
# Jobs created before this feature existed keep running in UTC (the
# `timezone` column defaults to 'UTC' via the database migration) so their
# behaviour doesn't silently shift when the bot is upgraded. Only new jobs
# pick up BOT_TIMEZONE.

BOT_TIMEZONE: str = os.getenv("BOT_TIMEZONE", "America/Bogota")

# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

GIT_SHA: str = os.getenv("GIT_SHA", "unknown")
HEARTBEAT_URL: str | None = os.getenv("HEARTBEAT_URL") or None
_START_TIME: datetime = datetime.now(tz=timezone.utc)

HEALTH_FILE: str = os.getenv("HEALTH_FILE", "/tmp/bot_healthy")
"""Sentinel file touched every few minutes by _touch_health_file, used by
Docker's HEALTHCHECK. Deliberately independent of HEARTBEAT_URL and of user
activity — a frozen event loop stops touching it even if no job or heartbeat
is configured, which a log-mtime-based healthcheck could miss during
legitimately idle periods."""


def _is_admin(user_id: int | None) -> bool:
    return user_id is not None and ADMIN_USER_ID is not None and user_id == ADMIN_USER_ID


async def _reject_unauthorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global gatekeeper — registered in ``group=-1`` so it runs before every
    other handler (commands, callbacks, ConversationHandler states alike).

    Allowed users pass through silently. Anyone else gets no reply at all —
    only a WARNING log line with their user_id, which is the intended way
    for the operator to discover their own ID on first contact.
    """
    user = update.effective_user
    user_id = user.id if user else None
    if user_id is not None and (user_id in ALLOWED_USER_IDS or _is_admin(user_id)):
        return
    _logger.warning("Rejected update from unauthorized user_id=%s", user_id)
    raise ApplicationHandlerStop


# ---------------------------------------------------------------------------
# ConversationHandler state constants
# ---------------------------------------------------------------------------

PLATFORM, QUERY, FREQUENCY, INTERVAL_DAYS, TIMES, PAGES, ADS, DURATION = range(8)

# ---------------------------------------------------------------------------
# MarkdownV2 helper
# ---------------------------------------------------------------------------

_MD2_SPECIAL = r"\_*[]()~`>#+-=|{}.!$:"


def _esc(text: str) -> str:
    """Escape special characters for MarkdownV2."""
    return re.sub(r"([\_\*\[\]\(\)\~\`\>\#\+\-\=\|\{\}\.\!\$\:])", r"\\\1", str(text))


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = ReplyKeyboardMarkup(
        [["/track", "/list"], ["/stop", "/help"]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )
    await update.message.reply_text(
        "Welcome to the automated ecommerce scraping engine. This bot allows "
        "you to deploy background tasks to monitor product availability and "
        "pricing across Amazon and MercadoLibre.\n\n"
        "Capabilities:\n"
        "  1. Deploy custom interval-based tracking jobs.\n"
        "  2. Bypass platform restrictions with headless browser execution.\n"
        "  3. Receive structured price reports directly in this chat.\n\n"
        "Press /track to deploy your first job, or /help for the full command "
        "reference.",
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/start  – Initialize the scraping engine\n"
        "/track  – Deploy a new tracking job\n"
        "/list   – View your active jobs\n"
        "/stop   – Cancel a running job\n"
        "/export – Download history for a job as an Excel file\n"
        "/runnow – Run one of your jobs immediately\n"
        "/health – Admin: operational status snapshot\n"
        "/diag   – Admin: probe Amazon access from this host\n"
        "/help   – Show this message"
    )


# ---------------------------------------------------------------------------
# /list
# ---------------------------------------------------------------------------


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    async with session_scope() as session:
        result = await session.execute(
            select(TrackedJob).where(
                TrackedJob.chat_id == chat_id,
                TrackedJob.is_active == True,  # noqa: E712
            )
        )
        jobs = result.scalars().all()

    if not jobs:
        await update.message.reply_text("No active tracking jobs.")
        return

    lines = ["<b>Active tracking jobs:</b>\n"]
    for job in jobs:
        platforms_str = ", ".join(p.capitalize() for p in job.get_platforms())
        times_str = ", ".join(job.get_execution_times()) + f" ({job.timezone})"
        expiry_str = job.expiration_date.strftime("%Y-%m-%d")
        lines.append(
            f"<code>#{job.id}</code> | {html.escape(job.query[:30])} | "
            f"{html.escape(platforms_str)} | {html.escape(job.schedule_type)} | "
            f"{html.escape(times_str)} | exp: {html.escape(expiry_str)}"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# /export — command + callback
# ---------------------------------------------------------------------------


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    async with session_scope() as session:
        result = await session.execute(
            select(TrackedJob)
            .where(TrackedJob.chat_id == chat_id)
            .order_by(TrackedJob.created_at.desc())
            .limit(10)
        )
        jobs = result.scalars().all()

    if not jobs:
        await update.message.reply_text("No tracking jobs found to export.")
        return

    buttons = [
        [InlineKeyboardButton(
            text=f"#{job.id} — {job.query[:30]} ({'active' if job.is_active else 'ended'})",
            callback_data=f"export:{job.id}",
        )]
        for job in jobs
    ]
    await update.message.reply_text(
        "Select a job to export its scrape history as Excel:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def export_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    job_id = int(query.data.split(":")[1])
    chat_id = str(update.effective_chat.id)

    async with session_scope() as session:
        result = await session.execute(select(TrackedJob).where(TrackedJob.id == job_id))
        job = result.scalar_one_or_none()

    if job is None or job.chat_id != chat_id:
        await query.edit_message_text("Job not found or access denied.")
        return

    async with session_scope() as session:
        result = await session.execute(
            select(ScrapedProduct)
            .join(ScrapeHistory, ScrapedProduct.history_id == ScrapeHistory.id)
            .where(ScrapeHistory.job_id == job_id)
            .order_by(ScrapeHistory.timestamp.asc(), ScrapedProduct.id.asc())
        )
        products = result.scalars().all()

    if not products:
        await query.edit_message_text(f"Job #{job_id} has no recorded products yet.")
        return

    import pandas as pd

    records = [
        {
            "id": p.product_id,
            "title": p.title,
            "price": p.price,
            "url": p.url,
            "source": p.source,
            "currency": p.currency,
            "rating": p.rating,
            "review_count": p.review_count,
        }
        for p in products
    ]
    df = pd.DataFrame(records)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        meta = pd.DataFrame([{
            "Job ID": job.id,
            "Query": job.query,
            "Platforms": ", ".join(job.get_platforms()),
            "Schedule": job.schedule_type,
            "Pages per run": job.pages_per_run,
            "Include ads": job.include_ads,
            "Active": job.is_active,
            "Expires": job.expiration_date.strftime("%Y-%m-%d"),
        }])
        meta.to_excel(writer, index=False, sheet_name="Job Info")
        df.to_excel(writer, index=False, sheet_name="Products")
        ws = writer.sheets["Products"]
        for col in ws.columns:
            max_len = max(len(str(cell.value or "")) for cell in col)
            ws.column_dimensions[col[0].column_letter].width = max(12, max_len + 2)

    output.seek(0)
    filename = f"history_job{job_id}_{job.query[:20].replace(' ', '_')}.xlsx"

    await query.edit_message_text(f"Generating export for Job #{job_id}…")
    await context.bot.send_document(
        chat_id=int(chat_id),
        document=output,
        filename=filename,
        caption=f"Job #{job_id} — {len(products)} product(s) | Query: {job.query[:40]}",
    )


# ---------------------------------------------------------------------------
# /stop — command + callback
# ---------------------------------------------------------------------------


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    async with session_scope() as session:
        result = await session.execute(
            select(TrackedJob).where(
                TrackedJob.chat_id == chat_id,
                TrackedJob.is_active == True,  # noqa: E712
            )
        )
        jobs = result.scalars().all()

    if not jobs:
        await update.message.reply_text("No active jobs to stop.")
        return

    buttons = [
        [
            InlineKeyboardButton(
                text=f"#{job.id} — {job.query[:30]}",
                callback_data=f"stop:{job.id}",
            )
        ]
        for job in jobs
    ]
    await update.message.reply_text(
        "Select the job to terminate:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def stop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    job_id = int(query.data.split(":")[1])
    chat_id = str(update.effective_chat.id)

    async with session_scope() as session:
        result = await session.execute(
            select(TrackedJob).where(TrackedJob.id == job_id)
        )
        job = result.scalar_one_or_none()

        if job is None or job.chat_id != chat_id:
            await query.edit_message_text("Job not found or access denied.")
            return

        job.is_active = False

    # Remove from JobQueue
    job_name = f"job_{job_id}"
    running_jobs = context.job_queue.get_jobs_by_name(job_name)
    for j in running_jobs:
        j.schedule_removal()

    await query.edit_message_text(f"Job #{job_id} terminated.")


# ---------------------------------------------------------------------------
# /track — ConversationHandler
# ---------------------------------------------------------------------------


async def track_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = str(update.effective_chat.id)
    async with session_scope() as session:
        result = await session.execute(
            select(TrackedJob).where(
                TrackedJob.chat_id == chat_id,
                TrackedJob.is_active == True,  # noqa: E712
            )
        )
        active_count = len(result.scalars().all())

    if active_count >= MAX_ACTIVE_JOBS_PER_USER:
        await update.message.reply_text(
            f"You already have {active_count} active job(s), which is the "
            f"maximum ({MAX_ACTIVE_JOBS_PER_USER}). Use /stop to free up a slot "
            "before creating a new one."
        )
        return ConversationHandler.END

    context.user_data.clear()
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Amazon only", callback_data="platform:amazon"),
                InlineKeyboardButton(
                    "MercadoLibre only", callback_data="platform:mercadolibre"
                ),
                InlineKeyboardButton("Both platforms", callback_data="platform:both"),
            ]
        ]
    )
    await update.message.reply_text(
        "Select the platform(s) to search:", reply_markup=keyboard
    )
    return PLATFORM


async def platform_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    choice = query.data.split(":")[1]
    if choice == "both":
        context.user_data["platforms"] = ["amazon", "mercadolibre"]
    else:
        context.user_data["platforms"] = [choice]

    await query.edit_message_text(
        "Enter your search term (e.g. Samsung Galaxy S24):"
    )
    return QUERY


async def query_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not MIN_QUERY_LENGTH <= len(text) <= MAX_QUERY_LENGTH:
        await update.message.reply_text(
            f"Search term must be between {MIN_QUERY_LENGTH} and "
            f"{MAX_QUERY_LENGTH} characters. Please try again:"
        )
        return QUERY

    context.user_data["query"] = text

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Daily", callback_data="freq:daily"),
                InlineKeyboardButton(
                    "Every N days", callback_data="freq:custom_days"
                ),
                InlineKeyboardButton("Weekly", callback_data="freq:weekly"),
            ]
        ]
    )
    await update.message.reply_text(
        "How often should this job run?", reply_markup=keyboard
    )
    return FREQUENCY


async def frequency_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    schedule_type = query.data.split(":")[1]
    context.user_data["schedule_type"] = schedule_type

    if schedule_type == "custom_days":
        await query.edit_message_text(
            "How many days between runs? Enter a number from 1 to 30:"
        )
        return INTERVAL_DAYS

    await query.edit_message_text(
        f"Enter run time(s) in HH:MM format ({BOT_TIMEZONE}), separated by commas.\n"
        "Examples: 08:00  or  08:00, 20:00"
    )
    return TIMES


async def interval_days_received(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    text = update.message.text.strip()
    try:
        days = int(text)
        if not 1 <= days <= 30:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Please enter a whole number between 1 and 30."
        )
        return INTERVAL_DAYS

    context.user_data["interval_days"] = days
    await update.message.reply_text(
        f"Enter run time(s) in HH:MM format ({BOT_TIMEZONE}), separated by commas.\n"
        "Examples: 08:00  or  08:00, 20:00"
    )
    return TIMES


async def times_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    tokens = [t.strip() for t in raw.split(",")]
    time_pattern = re.compile(r"^\d{2}:\d{2}$")

    valid_times: list[str] = []
    for token in tokens:
        if not time_pattern.match(token):
            await update.message.reply_text(
                f'Invalid format: "{token}". Expected HH:MM, e.g. 08:00'
            )
            return TIMES
        hour, minute = int(token[:2]), int(token[3:])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            await update.message.reply_text(
                f'Invalid time: "{token}". Hours must be 0-23, minutes 0-59.'
            )
            return TIMES
        valid_times.append(token)

    if len(valid_times) > MAX_TIMES_PER_JOB:
        await update.message.reply_text(
            f"A job can have at most {MAX_TIMES_PER_JOB} run time(s) per day. "
            f"You entered {len(valid_times)}. Please try again:"
        )
        return TIMES

    context.user_data["execution_times"] = valid_times
    await update.message.reply_text(
        "How many pages to scrape per run?\n"
        "`0` — all available\n"
        "`1`–`10` — limit to N pages (default: 2)"
    )
    return PAGES


async def pages_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        pages = int(text)
        if not 0 <= pages <= 10:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Please enter a whole number between 0 and 10 (0 = all pages)."
        )
        return PAGES

    user_id = update.effective_user.id if update.effective_user else None
    if pages == 0 and not _is_admin(user_id):
        await update.message.reply_text(
            "`0` (all pages) is reserved for the admin. Please enter a number "
            "between 1 and 10."
        )
        return PAGES

    context.user_data["pages_per_run"] = pages

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Include ads", callback_data="ads:yes"),
                InlineKeyboardButton("Exclude ads", callback_data="ads:no"),
            ]
        ]
    )
    await update.message.reply_text(
        "Include sponsored/advertised products in results?",
        reply_markup=keyboard,
    )
    return ADS


async def ads_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    choice = query.data.split(":")[1]
    context.user_data["include_ads"] = (choice == "yes")

    await query.edit_message_text(
        f"How many days should this job run before expiring? (1–{MAX_JOB_DURATION_DAYS}):"
    )
    return DURATION


async def duration_received(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    text = update.message.text.strip()
    try:
        days = int(text)
        if not 1 <= days <= MAX_JOB_DURATION_DAYS:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            f"Please enter a whole number between 1 and {MAX_JOB_DURATION_DAYS}."
        )
        return DURATION

    expiration_date = datetime.now(tz=timezone.utc) + timedelta(days=days)
    ud = context.user_data

    async with session_scope() as session:
        job = TrackedJob(
            chat_id=str(update.effective_chat.id),
            query=ud["query"],
            platforms=TrackedJob.encode_platforms(ud["platforms"]),
            schedule_type=ud["schedule_type"],
            execution_times=TrackedJob.encode_times(ud["execution_times"]),
            expiration_date=expiration_date,
            is_active=True,
            interval_days=ud.get("interval_days"),
            pages_per_run=ud.get("pages_per_run", 2),
            include_ads=ud.get("include_ads", True),
            timezone=BOT_TIMEZONE,
        )
        session.add(job)
        await session.flush()  # populate job.id before commit
        job_id = job.id

    # Re-fetch to get a fully attached object for scheduler registration
    async with session_scope() as session:
        result = await session.execute(
            select(TrackedJob).where(TrackedJob.id == job_id)
        )
        saved_job = result.scalar_one()
        _register_job_in_scheduler(context.application, saved_job)

    first_time = ud["execution_times"][0]
    await update.message.reply_text(
        f"Job #{job_id} deployed. First run at {first_time} {BOT_TIMEZONE}.",
        reply_markup=ReplyKeyboardRemove(),
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "Tracking setup cancelled.", reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


def build_track_conversation() -> ConversationHandler:
    # per_message=False is correct for menu-style inline keyboards where the
    # entire conversation lives in one chat context.  PTB still emits an
    # informational warning whenever CallbackQueryHandlers are present; we
    # suppress it here since the behaviour is intentional and understood.
    warnings.filterwarnings(
        "ignore",
        message=".*per_message=False.*CallbackQueryHandler.*",
        category=UserWarning,
    )
    return ConversationHandler(
        entry_points=[CommandHandler("track", track_start)],
        states={
            PLATFORM: [CallbackQueryHandler(platform_chosen, pattern=r"^platform:")],
            QUERY: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, query_received
                )
            ],
            FREQUENCY: [
                CallbackQueryHandler(frequency_chosen, pattern=r"^freq:")
            ],
            INTERVAL_DAYS: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, interval_days_received
                )
            ],
            TIMES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, times_received)
            ],
            PAGES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, pages_chosen)
            ],
            ADS: [CallbackQueryHandler(ads_chosen)],
            DURATION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, duration_received)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        allow_reentry=True,
        per_message=False,
    )


# ---------------------------------------------------------------------------
# Scheduled scrape task (JobQueue callback)
# ---------------------------------------------------------------------------


async def scheduled_scrape_task(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Called by JobQueue at each scheduled run time."""
    job_data: dict = context.job.data
    job_id: int = job_data["job_id"]
    chat_id: str = job_data["chat_id"]

    now_utc = datetime.now(tz=timezone.utc)

    # --- Load job from DB ---
    async with session_scope() as session:
        result = await session.execute(
            select(TrackedJob).where(TrackedJob.id == job_id)
        )
        job = result.scalar_one_or_none()

        if job is None or not job.is_active:
            context.job.schedule_removal()
            _logger.info("scheduled_scrape_task: job %d not found or inactive — removed", job_id)
            return

        # --- Expiry check ---
        expiry = job.expiration_date
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)

        if expiry < now_utc:
            job.is_active = False
            context.job.schedule_removal()
            _logger.info("scheduled_scrape_task: job %d expired — deactivated", job_id)

        platforms = job.get_platforms()
        query_text = job.query
        expiration_date = job.expiration_date
        is_expired = expiry < now_utc

    if is_expired:
        try:
            await context.bot.send_message(
                chat_id=int(chat_id),
                text=f"Job #{job_id} expired — tracking ended.",
            )
        except Exception as exc:
            _logger.warning("Could not notify user of job expiry: %s", exc)
        return

    # --- Execute scrape ---
    pages_per_run = job_data.get("pages_per_run", PAGES_PER_SCRAPE)
    include_ads = job_data.get("include_ads", True)
    result = await execute_scrape(
        query=query_text,
        platforms=platforms,
        pages=pages_per_run,
        include_ads=include_ads,
    )

    # --- Persist history + individual products ---
    async with session_scope() as session:
        history = ScrapeHistory(
            job_id=job_id,
            total_found=result["total_found"],
            lowest_price=result["lowest_price"],
        )
        session.add(history)
        await session.flush()  # populate history.id before inserting products
        for p in result.get("products", []):
            session.add(
                ScrapedProduct(
                    history_id=history.id,
                    product_id=p.get("id"),
                    title=p.get("title"),
                    price=p.get("price"),
                    url=p.get("url"),
                    source=p.get("source"),
                    currency=p.get("currency"),
                    rating=p.get("rating"),
                    review_count=p.get("review_count"),
                )
            )
    _logger.info(
        "scheduled_scrape_task: job %d — history saved, %d product(s) persisted",
        job_id,
        len(result.get("products", [])),
    )

    # --- Build & send report ---
    expiry_str = expiration_date.strftime("%Y-%m-%d") if isinstance(expiration_date, datetime) else str(expiration_date)
    platforms_display = ", ".join(p.capitalize() for p in platforms)

    if result["lowest_price"] is not None:
        price_str = _esc(f"${result['lowest_price']:.2f}")
    else:
        price_str = "N/A"

    ads_note = "\n🚫 _Sponsored products excluded_" if not include_ads else ""

    report = (
        f"🔍 *Job \\#{_esc(str(job_id))} — Price Report*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 *Query:* `{_esc(query_text)}`\n"
        f"🏪 *Platforms:* {_esc(platforms_display)}\n"
        f"📊 *Products found:* {_esc(str(result['total_found']))}\n"
        f"💰 *Lowest price:* {price_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"_Tracked until: {_esc(expiry_str)}_"
        f"{ads_note}"
    )

    try:
        await context.bot.send_message(
            chat_id=int(chat_id),
            text=report,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as exc:
        _logger.error("Could not send report for job %d: %s", job_id, exc)

    if result["errors"]:
        error_text = "⚠️ Partial errors during this run:\n" + "\n".join(
            f"  • {e}" for e in result["errors"]
        )
        try:
            await context.bot.send_message(chat_id=int(chat_id), text=error_text)
        except Exception as exc:
            _logger.warning("Could not send error notice for job %d: %s", job_id, exc)


# ---------------------------------------------------------------------------
# Scheduler registration helper
# ---------------------------------------------------------------------------


def _build_job_data(job: TrackedJob) -> dict:
    """Build the JobQueue ``data`` payload shared by scheduled runs and
    ``/runnow`` — keeps both call sites in sync on what ``scheduled_scrape_task``
    expects to find in ``context.job.data``."""
    return {
        "job_id": job.id,
        "chat_id": job.chat_id,
        "pages_per_run": job.pages_per_run,
        "include_ads": job.include_ads,
    }


def _register_job_in_scheduler(application: Application, job: TrackedJob) -> None:
    """Add a TrackedJob's run-times to the application's JobQueue.

    Safe to call multiple times for the same job (e.g., at startup during
    job recovery). Each call registers new JobQueue entries; the job name
    (``f"job_{job.id}"``) can be used to look up and remove them later.

    Args:
        application: The running PTB Application instance.
        job: A :class:`~database.TrackedJob` ORM object whose attributes are
            already committed and accessible (``expire_on_commit=False``).
    """
    job_name = f"job_{job.id}"
    job_data = _build_job_data(job)

    job_tz = ZoneInfo(job.timezone)

    for time_str in job.get_execution_times():
        hour, minute = map(int, time_str.split(":"))
        run_time = time(hour, minute, tzinfo=job_tz)

        if job.schedule_type == "daily":
            application.job_queue.run_daily(
                scheduled_scrape_task,
                run_time,
                name=job_name,
                data=job_data,
                chat_id=int(job.chat_id),
            )

        elif job.schedule_type == "weekly":
            # Run once per week — every 7 days at the specified time.
            # first=run_time schedules the first fire at the next occurrence
            # of that wall-clock time (UTC).
            application.job_queue.run_repeating(
                scheduled_scrape_task,
                interval=timedelta(weeks=1),
                first=run_time,
                name=job_name,
                data=job_data,
                chat_id=int(job.chat_id),
            )

        elif job.schedule_type == "custom_days":
            interval_days = job.interval_days or 1
            application.job_queue.run_repeating(
                scheduled_scrape_task,
                interval=timedelta(days=interval_days),
                first=run_time,
                name=job_name,
                data=job_data,
                chat_id=int(job.chat_id),
            )

        else:
            _logger.warning(
                "_register_job_in_scheduler: unknown schedule_type %r for job %d",
                job.schedule_type,
                job.id,
            )


# ---------------------------------------------------------------------------
# Observability — error handler, /runnow, /health, heartbeat
# ---------------------------------------------------------------------------


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler registered via ``Application.add_error_handler``.

    The full traceback always goes to the log (that's what post-mortems use).
    The admin additionally gets a short, truncated Telegram message with just
    enough to triage — exception type, originating job (if any), and the
    error text — never the traceback itself, which could leak internals.
    """
    _logger.error("Exception while handling an update: %s", update, exc_info=context.error)

    if ADMIN_USER_ID is None:
        return

    job_id = None
    if context.job is not None and isinstance(context.job.data, dict):
        job_id = context.job.data.get("job_id")

    parts = [f"⚠️ {type(context.error).__name__}"]
    if job_id is not None:
        parts.append(f"job #{job_id}")
    summary = " | ".join(parts) + f": {context.error}"
    summary = summary[:1000]

    try:
        await context.bot.send_message(chat_id=ADMIN_USER_ID, text=summary)
    except Exception as exc:
        _logger.warning("Could not notify admin of error: %s", exc)


async def cmd_runnow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run one of the caller's own jobs immediately, without waiting for its
    scheduled time. Useful for testing a job from the phone right after
    creating it."""
    chat_id = str(update.effective_chat.id)
    args = context.args

    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /runnow <job_id>")
        return

    job_id = int(args[0])
    async with session_scope() as session:
        result = await session.execute(select(TrackedJob).where(TrackedJob.id == job_id))
        job = result.scalar_one_or_none()

    if job is None or job.chat_id != chat_id:
        await update.message.reply_text("Job not found or access denied.")
        return

    context.job_queue.run_once(
        scheduled_scrape_task,
        when=0,
        data=_build_job_data(job),
        name=f"runnow_job_{job.id}",
        chat_id=int(job.chat_id),
    )
    await update.message.reply_text(f"Running job #{job_id} now…")


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only operational snapshot — uptime, active jobs, last
    successful scrape, build version, and outbound IP (useful to confirm
    which network the bot is actually scraping from)."""
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id):
        await update.message.reply_text("Admin only.")
        return

    uptime = datetime.now(tz=timezone.utc) - _START_TIME

    async with session_scope() as session:
        active_result = await session.execute(
            select(func.count()).select_from(TrackedJob).where(
                TrackedJob.is_active == True  # noqa: E712
            )
        )
        active_count = active_result.scalar_one()

        last_scrape_result = await session.execute(select(func.max(ScrapeHistory.timestamp)))
        last_scrape = last_scrape_result.scalar_one()

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get("https://checkip.amazonaws.com")
            public_ip = response.text.strip()
    except Exception as exc:
        _logger.warning("cmd_health: could not determine outbound IP: %s", exc)
        public_ip = "unknown"

    await update.message.reply_text(
        "🩺 *Health*\n"
        f"Uptime: {_esc(str(uptime).split('.')[0])}\n"
        f"Active jobs: {_esc(str(active_count))}\n"
        f"Last successful scrape: {_esc(str(last_scrape) if last_scrape else 'never')}\n"
        f"Version: {_esc(GIT_SHA)}\n"
        f"Outbound IP: {_esc(public_ip)}",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _heartbeat_task(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pings an external dead-man's-switch (e.g. healthchecks.io) so the
    operator is alerted by that service if the bot process dies — it stops
    checking in, rather than the bot having to detect its own death."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.get(HEARTBEAT_URL)
    except Exception as exc:
        _logger.warning("Heartbeat GET to HEARTBEAT_URL failed: %s", exc)


async def _touch_health_file(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Updates HEALTH_FILE's mtime so Docker's HEALTHCHECK can detect a
    frozen event loop. Registered unconditionally (unlike the HEARTBEAT_URL
    ping) so the container healthcheck works out of the box."""
    try:
        Path(HEALTH_FILE).touch()
    except OSError as exc:
        _logger.warning("Could not touch HEALTH_FILE %s: %s", HEALTH_FILE, exc)


async def cmd_diag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: runs a minimal (1 query, 1 round) version of
    scripts/probe_amazon.py in the background and reports a per-strategy
    ✅/⚠️/❌ summary. Shares core_engine's _browser_semaphore so this never
    races a scheduled job's own Playwright instance."""
    user_id = update.effective_user.id if update.effective_user else None
    if not _is_admin(user_id):
        await update.message.reply_text("Admin only.")
        return

    await update.message.reply_text("Running diagnostics — this can take a minute…")
    chat_id = update.effective_chat.id
    asyncio.create_task(_run_diag_and_report(context, chat_id))


async def _run_diag_and_report(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    from amazon_scraper import AmazonScraper
    from core_engine import _browser_semaphore
    from scripts.probe_amazon import ALL_STRATEGIES, get_public_ip, run_attempt

    try:
        public_ip = await get_public_ip()
        parser_helper = AmazonScraper()
        lines = [f"Outbound IP: {public_ip}"]

        async with _browser_semaphore:
            for strategy in ALL_STRATEGIES:
                attempt = await run_attempt(strategy, "kindle paperwhite", page=1, parser=parser_helper)
                if attempt.error:
                    lines.append(f"❌ {strategy}: {attempt.error}")
                elif attempt.block_reason:
                    lines.append(f"⚠️ {strategy}: {attempt.block_reason}")
                else:
                    lines.append(f"✅ {strategy}: {attempt.products_found} product(s)")

        await context.bot.send_message(chat_id=chat_id, text="\n".join(lines))
    except Exception as exc:
        _logger.error("cmd_diag: probe run failed: %s", exc)
        try:
            await context.bot.send_message(chat_id=chat_id, text=f"Diagnostics failed: {exc}")
        except Exception as send_exc:
            _logger.warning("cmd_diag: could not report failure to chat: %s", send_exc)


# ---------------------------------------------------------------------------
# Startup hooks
# ---------------------------------------------------------------------------


async def _restore_jobs_from_db(application: Application) -> None:
    """Re-register all active, non-expired jobs after a bot restart."""
    now_utc = datetime.now(tz=timezone.utc)
    async with session_scope() as session:
        result = await session.execute(
            select(TrackedJob).where(TrackedJob.is_active == True)  # noqa: E712
        )
        all_active = result.scalars().all()

    restored = 0
    for job in all_active:
        expiry = job.expiration_date
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry <= now_utc:
            # Expired while the bot was offline — deactivate silently.
            async with session_scope() as session:
                result = await session.execute(
                    select(TrackedJob).where(TrackedJob.id == job.id)
                )
                db_job = result.scalar_one_or_none()
                if db_job:
                    db_job.is_active = False
            continue

        _register_job_in_scheduler(application, job)
        restored += 1

    _logger.info("[cyan]%d active job(s) restored to scheduler[/cyan]", restored)


async def post_init(application: Application) -> None:
    """Called by PTB after the Application is built but before polling starts."""
    _logger.info("Initialising database…")
    await init_db()
    _logger.info("[green]Database ready[/green]")

    _logger.info("Recovering scheduled jobs from database…")
    await _restore_jobs_from_db(application)

    application.job_queue.run_repeating(
        _touch_health_file, interval=timedelta(minutes=5), first=0, name="health_touch"
    )

    if HEARTBEAT_URL:
        application.job_queue.run_repeating(
            _heartbeat_task, interval=timedelta(minutes=10), first=0, name="heartbeat"
        )
        _logger.info("[green]Heartbeat enabled[/green] — pinging HEARTBEAT_URL every 10 min")

    await application.bot.set_my_commands(
        [
            BotCommand("start", "Initialize the web scraping engine"),
            BotCommand("track", "Deploy a new background tracking job"),
            BotCommand("list", "Fetch all active database records"),
            BotCommand("stop", "Terminate a running scheduled job"),
            BotCommand("export", "Download scrape history as Excel"),
            BotCommand("runnow", "Run one of your jobs immediately"),
            BotCommand("health", "Admin: operational status snapshot"),
            BotCommand("diag", "Admin: probe Amazon access from this host"),
            BotCommand("help", "Access documentation and syntax guide"),
        ]
    )
    _logger.info("[green]Bot commands registered[/green]")
    _logger.info("[bold green]Bot is live — polling for updates[/bold green]")


# ---------------------------------------------------------------------------
# Application wiring & main
# ---------------------------------------------------------------------------


def build_application() -> Application:
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(TypeHandler(Update, _reject_unauthorized), group=-1)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CallbackQueryHandler(stop_callback, pattern=r"^stop:\d+$"))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CallbackQueryHandler(export_callback, pattern=r"^export:\d+$"))
    app.add_handler(CommandHandler("runnow", cmd_runnow))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("diag", cmd_diag))
    app.add_handler(build_track_conversation())

    app.add_error_handler(error_handler)

    return app


def main() -> None:
    banner = Text.assemble(
        ("Automated Ecommerce Scraping Engine\n", "bold cyan"),
        ("Telegram Bot  ·  ", "dim"),
        ("Amazon", "yellow"),
        ("  +  ", "dim"),
        ("MercadoLibre", "green"),
        ("  ·  Playwright-powered", "dim"),
    )
    _console.print(Panel(banner, border_style="blue", padding=(1, 4)))
    _logger.info("Building application…")
    app = build_application()
    _logger.info("Starting polling (drop_pending_updates=True)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
