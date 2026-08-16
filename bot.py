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
  TELEGRAM_BOT_TOKEN  — required, obtained from BotFather
  PAGES_PER_SCRAPE    — optional, default 2 (pages passed to execute_scrape)
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

from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.text import Text
from sqlalchemy import select
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
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
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
        times_str = ", ".join(job.get_execution_times())
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
    context.user_data["query"] = update.message.text.strip()

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
        "Enter run time(s) in HH:MM format (UTC), separated by commas.\n"
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
        "Enter run time(s) in HH:MM format (UTC), separated by commas.\n"
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
        "How many days should this job run before expiring? (1–90):"
    )
    return DURATION


async def duration_received(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    text = update.message.text.strip()
    try:
        days = int(text)
        if not 1 <= days <= 90:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Please enter a whole number between 1 and 90."
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
        f"Job #{job_id} deployed. First run at {first_time} UTC.",
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
    job_data = {
        "job_id": job.id,
        "chat_id": job.chat_id,
        "pages_per_run": job.pages_per_run,
        "include_ads": job.include_ads,
    }

    for time_str in job.get_execution_times():
        hour, minute = map(int, time_str.split(":"))
        run_time = time(hour, minute, tzinfo=timezone.utc)

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

    await application.bot.set_my_commands(
        [
            BotCommand("start", "Initialize the web scraping engine"),
            BotCommand("track", "Deploy a new background tracking job"),
            BotCommand("list", "Fetch all active database records"),
            BotCommand("stop", "Terminate a running scheduled job"),
            BotCommand("export", "Download scrape history as Excel"),
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

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CallbackQueryHandler(stop_callback, pattern=r"^stop:\d+$"))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CallbackQueryHandler(export_callback, pattern=r"^export:\d+$"))
    app.add_handler(build_track_conversation())

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
