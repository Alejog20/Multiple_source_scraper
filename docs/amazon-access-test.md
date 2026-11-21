# Amazon access test — how to run it and read the results

## Why this exists

Amazon treats traffic from datacenter IP ranges (AWS, GCP, Azure, etc.)
with far more suspicion than residential traffic. Before relying on the
EC2 deploy for scheduled scrapes, we need data on whether Amazon blocks
us from there, and if so, which fallback strategy in the resilience
funnel (httpx desktop → httpx mobile → Playwright) still gets through.

`scripts/probe_amazon.py` measures this directly, without touching the
scraper's cache, and without hammering Amazon — it's capped at under 60
requests per run, run sequentially with delays between each.

## Running it

From your local machine (residential IP, Bogotá):

```bash
uv run python scripts/probe_amazon.py
```

From the EC2 instance, once deployed:

```bash
cd /opt/scraper-bot
uv run python scripts/probe_amazon.py
```

Useful variations:

```bash
# Faster, smaller run — one query, one round, two strategies
uv run python scripts/probe_amazon.py --rounds 1 --queries "kindle paperwhite" \
  --strategies httpx_desktop curl_cffi_chrome

# Only the non-browser strategies (skip Playwright entirely)
uv run python scripts/probe_amazon.py --strategies httpx_desktop httpx_mobile curl_cffi_chrome
```

Each run prints a success-rate table to the console and writes a JSON
file to `probe_results/<hostname>_<UTC timestamp>.json` (gitignored —
these are local measurement artifacts, not something to commit).

Exit code tells you the headline result without reading the table:

| Exit code | Meaning |
|---|---|
| `0` | At least one non-browser strategy (httpx/curl_cffi) hit ≥70% success |
| `2` | Only a Playwright strategy hit ≥70% success |
| `1` | Nothing hit 70% — Amazon is blocking this host across the board |

## Interpreting the results

Run it **three times over 24–48 hours, at different times of day**, from
both locations, before drawing conclusions — a single run can be noise
(a transient CAPTCHA wave, a temporary IP reputation hit, etc.).

- **httpx or curl_cffi pass ≥70% on EC2** — nothing more to do. The
  funnel already tries these before falling back to Playwright, so
  scheduled jobs on EC2 will behave the same as they did on the old VM.
- **Only Playwright passes on EC2** — workable, but budget for it:
  Playwright is the heaviest strategy (full Chromium instance per page),
  and `core_engine._browser_semaphore` caps it at one concurrent instance
  specifically to protect a small instance's RAM. Watch `/health` and
  `docker stats` after a few days of real scheduled runs before trusting
  it long-term.
- **Nothing passes on EC2, but local (residential) passes** — this is an
  IP-reputation block, not a code problem. Don't try to "fix" it by
  retrying harder or removing delays — that makes the reputation worse.
  Options, roughly in order of effort:
  1. Reduce scrape frequency / page counts for Amazon jobs.
  2. Lean on MercadoLibre more (if the use case allows) — it isn't
     subject to the same datacenter-IP suspicion in testing so far.
  3. Route Amazon requests through a residential proxy. The scraper
     already has a `HTTPS_PROXY_AMAZON` environment variable reserved
     for this (unused for now — wire it into `AmazonScraper`'s httpx
     client only once a specific proxy provider is chosen and the cost
     is justified by the data from this probe).

## What `curl_cffi_chrome` is doing in the probe but not in the scraper

MercadoLibreScraper already uses `curl_cffi` with Chrome TLS/JA3
impersonation as one of its funnel strategies — `AmazonScraper` doesn't.
The probe includes a `curl_cffi_chrome` strategy specifically to collect
evidence on whether it would help Amazon too. If the data shows it
clearing 70%+ where plain httpx doesn't, that's a strong case for adding
it to `AmazonScraper`'s funnel (between the mobile httpx attempt and the
Playwright fallback) — but that's a separate, evidence-driven change,
not something this probe does on its own.

## `/diag` — the same check from inside Telegram

Admins can run `/diag` from the bot itself for a quick, in-chat version
of this probe (one query, one round, all strategies). It's meant for a
fast sanity check between full probe runs, not a replacement for them —
it doesn't write a JSON file or compute success rates across multiple
attempts.
