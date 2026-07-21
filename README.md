# Multi-Platform E-Commerce Scraper — Version 4.1


### Who is this for? 

Individuals or teams who need price monitoring. 

## 1. Project Status & Overview

A multi-platform price-tracking system built in Python. It exposes two interfaces — a rich CLI (`main.py`) for one-shot searches and history exports, and a Telegram bot (`bot.py`) for persistent, scheduled background jobs with in-chat XLSX exports — both powered by the same `core_engine.py` business logic layer.

The scrapers employ a 5-layer resilience funnel per page, validate all extracted data, filter results by query relevance, and natively detect and optionally filter sponsored/advertised listings.

---

### What's New in Version 4.1

**Query-relevance filter (`core_engine.py`):**
- After scraping and validation, results are now filtered to keep only products whose title contains **all significant keywords** from the search query.
- Eliminates noise caused by broad brand/category matches — e.g., searching "Sigma 18-50 mm" no longer returns bare "Sigma 30mm" or "Sigma 85mm" listings.
- Uses case-insensitive substring matching so compound spec tokens like "18-50" correctly match titles that write it as "18-50mm".
- Common stop-words (English and Spanish) are excluded from the keyword set; single-token queries bypass the filter entirely.
- Removed count is reported in the application log at `INFO` level.

**`scraped_at` timestamp column — CLI Mode 2 Products sheet:**
- The "Products" sheet in the CLI history export now includes a `scraped_at` column as the first column (column A).
- Format: `MM/DD/YYYY HH:MM` (UTC), sourced from the `ScrapeHistory.timestamp` of the run that produced each product row.
- Enables time-series price analysis directly inside Excel when a job has been scraped multiple times.

---

### What's New in Version 4.0

**History XLSX Export — Bot `/export` command:**
- New `/export` command shows an inline keyboard listing the user's 10 most recent jobs (both active and ended).
- Selecting a job causes the bot to send that job's full `scrape_history` as an in-memory XLSX document directly in chat — no temp files, no disk I/O on the server.
- The XLSX contains two sheets: **"Job Info"** (one row of job metadata) and **"Scrape History"** (one row per scheduled run with timestamp, products found, and lowest price). Column widths are auto-fitted.
- Ownership is verified: users can only export their own jobs.

**History XLSX Export — CLI mode menu:**
- `main.py` now opens with a top-level mode prompt: **1 = Search products** (existing flow, unchanged) or **2 = Export scrape history to Excel**.
- Mode 2 lists all jobs from the local database, lets the user pick one by ID, and writes the same two-sheet XLSX to disk.

---

### What's New in Version 3.0

**Auto-Pagination:**
- Both scrapers now stop as soon as any page returns zero results, rather than running through all requested pages unnecessarily.
- A new "all pages" mode (`pages=0` in CLI, `0` in the bot) automatically pages until the site returns nothing, using a 50-page safety ceiling.

**Ad / Sponsored Product Filtering:**
- Every product is now tagged with `is_ad: bool` at extraction time.
- Amazon: detected via `.puis-sponsored-label-info-icon`, `.s-sponsored-label-info-icon`, and `aria-label="Sponsored"/"Patrocinado"` CSS selectors.
- MercadoLibre: detected via `position_type == "advertising"` / `is_advertising` in the API response; `click1.mercadolibre` URL pattern and `is-advertising` CSS class in HTML paths.
- `execute_scrape(include_ads=False)` filters tagged products after scraping.
- CLI prompts whether to include or exclude ads. Bot `/track` conversation includes an inline keyboard for the same choice.

**Telegram Bot — Scheduled Price Tracking:**
- Users deploy background jobs via `/track`. Each job stores its own `pages_per_run` and `include_ads` settings.
- Scheduled scrapes run at user-defined times (daily, weekly, or every N days), persist history to SQLite, and deliver formatted MarkdownV2 price reports to the user's chat.
- Reports append "🚫 Sponsored products excluded" when ads are filtered.

**Single Scrape Entry Point:**
- `core_engine.execute_scrape()` is now the only way all callers (CLI, bot scheduler, tests) invoke scraping. Direct scraper instantiation in `main.py` has been removed.

---

### What's New in Version 2.0

**Revolutionary Anti-Detection:**
- Playwright-Stealth integration: browser fingerprint masking.
- Enhanced browser headers: `sec-ch-ua` realistic fingerprinting.
- Intelligent retry logic: exponential backoff with smart error classification.
- Dynamic user-agent pools: latest browser versions with mobile/desktop variants.

**Critical Parser Fixes:**
- MercadoLibre title extraction: fixed to correctly extract from `img[title]` attributes.
- Enhanced selector robustness: multiple fallback selectors for all data points.
- Advanced price parsing: handles Colombian peso format and complex price structures.
- URL & ID extraction: improved pattern matching for product identification.

**Architecture:**
- Modern Python typing throughout the codebase.
- Comprehensive data validation with field-level type checking.
- Modular design with clear separation of concerns.

---

## 2. Core Architecture: The 5-Layer Resilience Funnel

For every page it needs to scrape, each scraper follows this sequence and stops as soon as it retrieves valid data.

1. **Cache First:** Checks a local `.cache` folder for recent results to avoid redundant requests.
2. **API Request (MercadoLibre only):** Fetches clean JSON directly from MercadoLibre's public API.
3. **Enhanced Desktop HTTPX Request:** Sophisticated requests with modern browser headers and exponential backoff.
4. **Mobile HTTPX Request:** Mobile browser simulation, often bypassing desktop-focused detection.
5. **Advanced Stealth Browser Emulation:** Playwright-stealth with anti-detection scripts, realistic viewport, and human-like delays.

**Stop condition (v3.0):** If a page returns zero products, the scraper stops immediately regardless of how many pages were requested. This applies both to mid-run "content exhausted" situations and to hard failures on page 1.

---

## 3. System Architecture

```
┌─────────────────────────────────────────────────────────┐
│                        Interfaces                       │
│                                                         │
│    CLI (main.py)              Telegram Bot (bot.py)     │
│    • Mode 1: One-shot search  • /track conversation     │
│    • Platform select          • Scheduled JobQueue      │
│    • pages=0 (all)            • pages per job           │
│    • include_ads prompt       • ads inline keyboard     │
│    • Mode 2: Export history   • /export → XLSX in chat  │
└──────────────────┬───────────────────┬──────────────────┘
                   │                   │
                   ▼                   ▼
        ┌─────────────────────────────────────┐
        │         core_engine.py              │
        │   execute_scrape(query, platforms,  │
        │       pages, include_ads)           │
        │   • pages=0 → _MAX_AUTO_PAGES (50)  │
        │   • sequential platform loop        │
        │   • dedup → validate → relevance     │
        │     filter → filter ads             │
        │   • _browser_semaphore (cap=1)       │
        └────────────┬──────────────┬─────────┘
                     │              │
          ┌──────────▼──┐     ┌─────▼────────────┐
          │amazon_scraper│    │mercadolibre_scraper│
          │  is_ad via   │    │  is_ad via API     │
          │  CSS labels  │    │  field / URL / CSS │
          └─────────────┘     └──────────────────┘
                                       │
                          ┌────────────▼────────────┐
                          │       database.py        │
                          │  TrackedJob              │
                          │  • pages_per_run         │
                          │  • include_ads           │
                          │  ScrapeHistory           │
                          └─────────────────────────┘
```

---

## 4. Data Flow Diagram — CLI

```plaintext
+-----------------------------+
|   Start: uv run python      |
|         main.py             |
+-----------------------------+
             |
             v
+-----------------------------+
|  Dependency Check & Setup   |
+-----------------------------+
             |
             v
+-----------------------------+
|  Mode Menu                  |
|  1. Search products         |
|  2. Export scrape history   |
+-----------------------------+
             |
      +------+------+
      |             |
  Mode 1        Mode 2
      |             |
      v             v
+----------+  +----------------------------+
|  Get     |  |  Load all TrackedJobs      |
|  User    |  |  from DB; display list     |
|  Input   |  |  → pick Job ID             |
+----------+  |  → fetch ScrapeHistory     |
      |       |  → write two-sheet .xlsx   |
      v       |    (Job Info + History)    |
+-----------------------------+  +--------+
|  core_engine.execute_scrape |
|  (single entry point)       |
+-----------------------------+
             |
             | For each platform, for each page...
             v
+--------------------------------------------------+
|           Execute 5-Layer Strategy Funnel          |
|--------------------------------------------------|
| 1. Cache Check -> [Hit?] --(Yes)--> Success      |
|      | (Miss)                                    |
|      v                                           |
| 2. API Request (ML only) -> [OK?] --> Success    |
|      | (Fail/Skip)                               |
|      v                                           |
| 3. Desktop HTTPX -> [Valid?] --> Success         |
|      | (Fail/Blocked)                            |
|      v                                           |
| 4. Mobile HTTPX -> [Valid?] --> Success          |
|      | (Fail/Blocked)                            |
|      v                                           |
| 5. Stealth Playwright -> [Valid?] --> Success    |
|      | (Fail)                                    |
|      v                                           |
| [Page empty / failed] --> stop if 0 results      |
+--------------------------------------------------+
             |
             v
+-----------------------------+
|  Deduplicate + Validate     |
|  Query-Relevance Filter     |
|  Filter ads if requested    |
+-----------------------------+
             |
             v
+-----------------------------+
|  Display Results Table      |
+-----------------------------+
             |
             v
+-----------------------------+
|  Prompt: Export to Excel?   |
+-----------------------------+
             |
        (Yes)|
             v
+-----------------------------+
|  Export to .xlsx            |
+-----------------------------+
```

---

## 5. Project Structure (File Breakdown)

| File | Purpose |
|---|---|
| `main.py` | CLI entry point. Opens with a mode menu (1 = search, 2 = export history). Mode 1: collects user input, calls `execute_scrape`, displays results, optional Excel export. Mode 2: lists all DB jobs, writes a two-sheet XLSX to disk. |
| `core_engine.py` | Business logic layer — the single scrape entry point for all callers. Orchestrates platform execution sequentially, handles `pages=0` auto mode, deduplicates, validates, filters by query relevance, filters ads. Module-level `_browser_semaphore` prevents concurrent Playwright instances. |
| `amazon_scraper.py` | `AmazonScraper` class. 5-layer funnel (cache → desktop HTTPX → mobile HTTPX → Playwright). Tags each product with `is_ad` via sponsored label CSS selectors. |
| `mercadolibre_scraper.py` | `MercadoLibreScraper` class. 5-layer funnel (cache → API → curl_cffi HTML → Playwright). Tags `is_ad` via API fields, URL pattern, and `is-advertising` CSS class. |
| `bot.py` | Telegram bot. ConversationHandler for `/track` (8 states). Scheduled JobQueue with per-job settings. MarkdownV2 price reports. `/export` command sends job history as an in-memory XLSX document to chat. |
| `database.py` | SQLAlchemy 2.x async persistence. `TrackedJob` (includes `pages_per_run`, `include_ads`) and `ScrapeHistory` models. Auto-migration via `_apply_migrations`. |
| `debug_utils.py` | Shared utilities: browser fingerprinting, retry logic, user-agent pools, file cache, data validation, logging infrastructure. **Do not modify.** |
| `tests/` | Unittest framework for offline validation against static HTML fixtures. |
| `agent_docs/AGENT_DOCS.md` | AI agent context file — documents architecture, conventions, and current feature state for AI-assisted development sessions. |

---

## 6. Setup & Usage

### Prerequisites
- Python 3.11+
- `uv` (recommended) or `pip`
- A Telegram bot token from [@BotFather](https://t.me/BotFather) (bot mode only)

### Step 1: Clone
```bash
git clone https://github.com/Alejog20/Ethical_amazon_scraper
cd Ethical_amazon_scraper
```

### Step 2: Install dependencies
```bash
# With uv (recommended)
uv sync

# Or with pip
pip install -r requirements.txt
```

### Step 3: Install Playwright browsers
```bash
playwright install chromium
```

### Step 4: Configure environment (bot mode only)
Create a `.env` file in the project root:
```env
TELEGRAM_BOT_TOKEN=your_token_here
PAGES_PER_SCRAPE=2        # fallback default if job has no per-job setting
DB_ECHO=false             # set to true to log all SQL
```

---

## 7. Running the CLI

```bash
uv run python main.py
```

You will first be asked which mode to use:

```
What would you like to do?
  1. Search products
  2. Export scrape history to Excel
```

**Mode 1 — Search products:** you will be prompted for:
1. **Platform** — Amazon (1), MercadoLibre (2), or Both (3)
2. **Search query** — e.g. `laptop gaming`
3. **Pages** — `0` = all available pages (stops when empty), `1–10` = fixed limit
4. **Include ads?** — `y` keeps sponsored results; `n` filters them out

Results are shown in a sorted table (top 30 by price). An optional Excel export is offered at the end.

**Mode 2 — Export scrape history:** displays all tracked jobs from the local database (active and ended), prompts for a Job ID, and writes a two-sheet `.xlsx` file to the current directory:
- **Sheet 1 "Job Info"** — one row of job metadata (ID, query, platforms, schedule, pages per run, include ads, active status, expiry date)
- **Sheet 2 "Products"** — one row per scraped product across all runs for that job, with columns: `scraped_at` (MM/DD/YYYY HH:MM UTC), `id`, `title`, `price`, `url`, `source`, `currency`, `rating`, `review_count`. The `scraped_at` column makes it straightforward to compare prices over time for the same product across multiple scrape runs.

---

## 8. Running the Telegram Bot

```bash
uv run python bot.py
```

### Bot Commands

| Command | Description |
|---|---|
| `/start` | Initialize the bot and show the command keyboard |
| `/track` | Deploy a new background tracking job (8-step wizard) |
| `/list` | View all your active jobs |
| `/stop` | Cancel a running job via inline keyboard |
| `/export` | Download scrape history for a job as an Excel file |
| `/help` | Show command reference |

### `/export` Flow

```
/export
  └─ Inline keyboard lists up to 10 most recent jobs (active + ended)
  └─ Tap a job → bot sends a .xlsx document to the chat
       Sheet 1 "Job Info"      — metadata row (query, platforms, schedule, …)
       Sheet 2 "Scrape History"— one row per run (timestamp, products, price)
```

Ownership is enforced: you can only export jobs you created. Jobs with no history yet return an informational message instead of a file.

### `/track` Conversation Flow

```
/track
  └─ Platform?        [Amazon] [MercadoLibre] [Both]
  └─ Search term:     (free text)
  └─ Frequency?       [Daily] [Every N days] [Weekly]
  └─ Interval days?   (only for "Every N days") 1–30
  └─ Run time(s)?     HH:MM, HH:MM  (UTC)
  └─ Pages per run?   0=all, 1–10
  └─ Include ads?     [Include ads] [Exclude ads]
  └─ Duration (days)? 1–90
```

The bot schedules scrapes at the specified UTC times and sends a MarkdownV2 price report to your chat after each run. If ads are excluded, the report appends "🚫 Sponsored products excluded."

---

## 9. Running the Tests

```bash
uv run python -m unittest tests/test_scheduler.py -v
```

The test suite validates:
- HTML parsing logic against static fixtures
- Data validation and edge case handling (missing prices, CAPTCHA pages)
- Scheduler job registration and recovery
- `execute_scrape` integration via mocked scrapers

---

## 10. Technical Implementation Details

### Ad Detection Strategy

| Platform | Method | Field |
|---|---|---|
| Amazon | CSS: `.puis-sponsored-label-info-icon` | `is_ad: bool` |
| Amazon | CSS: `.s-sponsored-label-info-icon` | |
| Amazon | Attr: `aria-label="Sponsored"` / `"Patrocinado"` | |
| MercadoLibre API | `position_type == "advertising"` | `is_ad: bool` |
| MercadoLibre API | `is_advertising` field | |
| MercadoLibre HTML (JSON) | URL contains `click1.mercadolibre` | |
| MercadoLibre HTML (fallback) | CSS class `is-advertising` on container | |

### Pagination Stop Logic
Both scrapers break out of their page loop when a page returns zero products. This means `pages=0` (auto mode) reliably terminates at the end of real content without hard-coding a limit, while `pages=N` terminates at whichever comes first: N pages fetched or the first empty page.

### Query-Relevance Filter

After deduplication and validation, `_filter_by_query_relevance()` removes products whose title does not contain every significant keyword from the original search query.

| Step | Detail |
|---|---|
| Tokenisation | Split query on whitespace/punctuation; lowercase all tokens |
| Stop-word removal | Skip common English/Spanish filler words (`the`, `and`, `de`, `con`, …) |
| Length guard | Skip tokens shorter than 2 characters |
| Single-keyword bypass | If fewer than 2 meaningful keywords remain, the filter is skipped entirely |
| Matching | Each keyword must appear as a **substring** of the title (case-insensitive). Substring (not word-boundary) matching ensures tokens like `18-50` match compound strings like `18-50mm` |
| Logging | Number of removed products is logged at `INFO` level: `filter_by_query_relevance: removed=X, kept=Y, query='…'` |

**Example — query `"Sigma 18-50 mm"`:** keywords → `["sigma", "18-50", "mm"]`

| Title | Kept? |
|---|---|
| Sigma 18-50mm f/2.8 DC DN Contemporary | ✓ |
| Sigma 18-50mm Art Lens for Sony E | ✓ |
| Sigma 30mm f/1.4 DC DN | ✗ (missing `18-50`) |
| Sigma 85mm f/1.4 Art | ✗ (missing `18-50`) |
| Sigma Lens Cap | ✗ (missing `18-50` and `mm`) |

### Concurrency Safety
`core_engine._browser_semaphore` (capacity 1) ensures only one `execute_scrape` call runs at a time across the whole process. This prevents concurrent Playwright instances from causing OOM kills on constrained servers. Increasing the capacity to 2 is the only change needed when moving to a higher-RAM server.

### Database Schema

```sql
tracked_jobs
  id              INTEGER PRIMARY KEY
  chat_id         TEXT
  query           TEXT
  platforms       TEXT (JSON array)
  schedule_type   TEXT  -- 'daily' | 'weekly' | 'custom_days'
  execution_times TEXT (JSON array of 'HH:MM')
  interval_days   INTEGER (nullable)
  pages_per_run   INTEGER DEFAULT 2
  include_ads     BOOLEAN DEFAULT 1
  expiration_date DATETIME
  is_active       BOOLEAN
  created_at      DATETIME

scrape_history
  id            INTEGER PRIMARY KEY
  job_id        INTEGER FK → tracked_jobs.id
  total_found   INTEGER
  lowest_price  FLOAT (nullable)
  timestamp     DATETIME
```

Schema migrations run automatically on startup via `database._apply_migrations` — existing databases gain the new columns without manual intervention.

---

## 11. Changelog

### Version 4.1
- **Query-relevance filter:** `core_engine._filter_by_query_relevance()` strips results whose title doesn't contain all meaningful query keywords, eliminating brand-only noise (e.g. bare "Sigma" results when searching "Sigma 18-50 mm"). Uses substring matching; single-token queries bypass the filter.
- **`scraped_at` column in Products sheet:** CLI Mode 2 export now writes the UTC scrape timestamp (`MM/DD/YYYY HH:MM`) as the first column of the "Products" sheet, enabling price-trend analysis across multiple scrape runs directly in Excel.
- **README fix:** corrected CLI Mode 2 sheet name from "Scrape History" to "Products" and updated its column list.

### Version 4.0
- **Bot `/export` command:** inline keyboard of the user's 10 most recent jobs; sends a two-sheet XLSX (Job Info + Scrape History) as an in-memory document directly in chat. Ownership check prevents cross-user access.
- **CLI mode menu:** `main.py` opens with a choice between "Search products" (existing flow) and "Export scrape history to Excel" (new `export_history()` function writes a matching two-sheet XLSX to disk).

### Version 3.0
- **Auto-pagination:** scrapers stop on first empty page; `pages=0` mode added to CLI and bot.
- **Ad filtering:** `is_ad` tagging in both scrapers; `execute_scrape(include_ads=False)` post-scrape filter; CLI prompt; bot inline keyboard; report footer note.
- **`core_engine` as single entry point:** `main.py` no longer instantiates scrapers directly.
- **Database:** `pages_per_run` and `include_ads` columns added to `tracked_jobs`; auto-migration on startup.
- **Bot conversation:** PAGES and ADS states inserted between TIMES and DURATION (8 total states).

### Version 2.0
- Playwright-stealth integration and enhanced anti-detection.
- Fixed MercadoLibre title extraction from `img[title]` attributes.
- Intelligent retry logic with exponential backoff.
- Comprehensive data validation and offline unittest framework.

### Version 0.1.0
- Initial multi-platform scraper with Amazon and MercadoLibre support.

---

## 12. Disclaimer & Ethical Use

**For Educational Purposes Only.** This software is provided "as is" for educational and research purposes to demonstrate advanced, resilient web scraping techniques.

**User Responsibility.** The user assumes all responsibility for compliance with the Terms of Service of any website scraped. The developers do not condone unethical use.

**Respectful Scraping.** Caching, random delays, and rotating user-agents are built in to minimize server impact. Use this tool responsibly.
