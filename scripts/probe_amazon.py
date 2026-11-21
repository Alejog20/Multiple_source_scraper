"""
scripts/probe_amazon.py — Amazon Access Diagnostic Probe
===========================================================
Measures, with data, whether Amazon blocks (CAPTCHA, 503, empty pages)
traffic from the host this script runs on, and which resilience-funnel
strategy actually works from here. Meant to be run once from a residential
connection and once from the target EC2 instance, so the two can be
compared — see docs/amazon-access-test.md for how to read the results.

This script is intentionally separate from AmazonScraper's own funnel: it
never touches the FileCache (a probe that could silently return cached
data would defeat the whole point), and it never runs strategies
concurrently — this project treats scraping as something to do politely,
not something to hammer a target with, even for diagnostics.

Usage:
    uv run python scripts/probe_amazon.py
    uv run python scripts/probe_amazon.py --rounds 1 --queries "kindle paperwhite"
    uv run python scripts/probe_amazon.py --strategies httpx_desktop curl_cffi_chrome
"""

import argparse
import asyncio
import json
import random
import socket
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Ensure the project root is on sys.path when running from scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from curl_cffi.requests import AsyncSession as CurlSession
from playwright.async_api import async_playwright
from rich.console import Console
from rich.table import Table

from amazon_scraper import AmazonScraper, detect_amazon_block
from debug_utils import get_random_user_agent, get_realistic_headers

try:
    from playwright_stealth import stealth_async
except ImportError:  # pragma: no cover — playwright-stealth is a pinned dependency
    stealth_async = None

console = Console()

NON_BROWSER_STRATEGIES = frozenset({"httpx_desktop", "httpx_mobile", "curl_cffi_chrome"})
ALL_STRATEGIES = [
    "httpx_desktop",
    "httpx_mobile",
    "curl_cffi_chrome",
    "playwright_stealth",
    "playwright_plain",
]
DEFAULT_QUERIES = ["sony wh-1000xm5", "kindle paperwhite", "logitech mx master 3s"]
MAX_TOTAL_REQUESTS = 60
IMDSV2_TOKEN_URL = "http://169.254.169.254/latest/api/token"
IMDSV2_REGION_URL = "http://169.254.169.254/latest/meta-data/placement/region"


@dataclass
class Attempt:
    strategy: str
    query: str
    page: int
    status: Optional[int]
    final_url: Optional[str]
    html_size: int
    block_reason: Optional[str]
    products_found: int
    latency_ms: float
    error: Optional[str]


def _amazon_url(query: str, page: int, is_mobile: bool = False) -> str:
    q = query.replace(" ", "+")
    if is_mobile:
        return f"https://www.amazon.com/gp/aw/s?k={q}&page={page}"
    return f"https://www.amazon.com/s?k={q}&page={page}"


async def _fetch_httpx(query: str, page: int, is_mobile: bool):
    url = _amazon_url(query, page, is_mobile)
    headers = get_realistic_headers(is_mobile)
    async with httpx.AsyncClient(http2=True, timeout=30.0, follow_redirects=True) as client:
        response = await client.get(url, headers=headers)
        return response.status_code, str(response.url), response.text


async def _fetch_curl_cffi_chrome(query: str, page: int):
    url = _amazon_url(query, page)
    async with CurlSession() as session:
        response = await session.get(url, impersonate="chrome", timeout=30)
        return response.status_code, str(response.url), response.text


async def _fetch_playwright(query: str, page: int, use_stealth: bool):
    url = _amazon_url(query, page)
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            context = await browser.new_context(
                user_agent=get_random_user_agent(is_mobile=False),
                viewport={"width": 1366, "height": 768},
            )
            pw_page = await context.new_page()
            if use_stealth and stealth_async is not None:
                await stealth_async(pw_page)
            await pw_page.goto(url, timeout=45000, wait_until="domcontentloaded")
            await asyncio.sleep(random.uniform(2, 4))
            html = await pw_page.content()
            return None, pw_page.url, html
        finally:
            await browser.close()


STRATEGY_FUNCS = {
    "httpx_desktop": lambda q, p: _fetch_httpx(q, p, is_mobile=False),
    "httpx_mobile": lambda q, p: _fetch_httpx(q, p, is_mobile=True),
    "curl_cffi_chrome": _fetch_curl_cffi_chrome,
    "playwright_stealth": lambda q, p: _fetch_playwright(q, p, use_stealth=True),
    "playwright_plain": lambda q, p: _fetch_playwright(q, p, use_stealth=False),
}


async def run_attempt(strategy: str, query: str, page: int, parser: AmazonScraper) -> Attempt:
    start = time.monotonic()
    try:
        status, final_url, html = await STRATEGY_FUNCS[strategy](query, page)
        latency_ms = (time.monotonic() - start) * 1000
        block_reason = detect_amazon_block(html, status or 200, final_url or "")
        products: list = []
        if block_reason is None:
            products, _ = parser._parse_html(html, is_mobile=(strategy == "httpx_mobile"))
        return Attempt(
            strategy=strategy,
            query=query,
            page=page,
            status=status,
            final_url=final_url,
            html_size=len(html),
            block_reason=block_reason,
            products_found=len(products),
            latency_ms=round(latency_ms, 1),
            error=None,
        )
    except Exception as exc:
        latency_ms = (time.monotonic() - start) * 1000
        return Attempt(
            strategy=strategy,
            query=query,
            page=page,
            status=None,
            final_url=None,
            html_size=0,
            block_reason=None,
            products_found=0,
            latency_ms=round(latency_ms, 1),
            error=f"{type(exc).__name__}: {exc}",
        )


async def get_public_ip() -> str:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get("https://checkip.amazonaws.com")
            return response.text.strip()
    except Exception:
        return "unknown"


async def get_ec2_context() -> dict:
    """Best-effort IMDSv2 probe. Fast-fails ('no-EC2') off this host when
    not running on EC2 — the 1s timeout keeps this cheap everywhere else."""
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            token_resp = await client.put(
                IMDSV2_TOKEN_URL,
                headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
            )
            token = token_resp.text
            region_resp = await client.get(
                IMDSV2_REGION_URL,
                headers={"X-aws-ec2-metadata-token": token},
            )
            return {"on_ec2": True, "region": region_resp.text}
    except Exception:
        return {"on_ec2": False, "region": "no-EC2"}


def _is_success(attempt: Attempt) -> bool:
    return (
        attempt.error is None
        and attempt.block_reason is None
        and attempt.products_found > 0
        and (attempt.status is None or attempt.status == 200)
    )


async def run_probe(
    queries: list[str], rounds: int, delay: float, strategies: list[str], result_dir: Path
) -> int:
    total_requests = len(queries) * rounds * len(strategies)
    if total_requests >= MAX_TOTAL_REQUESTS:
        console.print(
            f"[bold red]Aborting:[/bold red] this run would send {total_requests} requests "
            f"(limit: {MAX_TOTAL_REQUESTS}). Reduce --queries/--rounds/--strategies."
        )
        return 1

    console.rule("[bold cyan]Amazon Access Probe[/bold cyan]")
    public_ip = await get_public_ip()
    hostname = socket.gethostname()
    ec2_ctx = await get_ec2_context()

    console.print(f"[bold]Public IP:[/bold] {public_ip}")
    console.print(f"[bold]Hostname:[/bold] {hostname}")
    console.print(f"[bold]On EC2:[/bold] {ec2_ctx['on_ec2']}   [bold]Region:[/bold] {ec2_ctx['region']}")
    console.print(f"[dim]Planned requests: {total_requests} (cache is never read)[/dim]\n")

    parser_helper = AmazonScraper()
    attempts: list[Attempt] = []

    with console.status("[bold green]Probing…[/bold green]") as status_display:
        for round_num in range(1, rounds + 1):
            for query in queries:
                for strategy in strategies:
                    status_display.update(
                        f"[bold green]Round {round_num}/{rounds} — {strategy} — {query!r}[/bold green]"
                    )
                    attempts.append(await run_attempt(strategy, query, page=1, parser=parser_helper))
                    await asyncio.sleep(delay + random.uniform(0, delay * 0.3))

    table = Table(title="Success rate by strategy", show_lines=True)
    table.add_column("Strategy", style="cyan")
    table.add_column("Attempts", justify="right")
    table.add_column("Success", justify="right")
    table.add_column("Rate", justify="right")

    success_rates: dict[str, float] = {}
    for strategy in strategies:
        strategy_attempts = [a for a in attempts if a.strategy == strategy]
        successes = [a for a in strategy_attempts if _is_success(a)]
        rate = (len(successes) / len(strategy_attempts) * 100) if strategy_attempts else 0.0
        success_rates[strategy] = rate
        table.add_row(strategy, str(len(strategy_attempts)), str(len(successes)), f"{rate:.0f}%")

    console.print()
    console.print(table)

    result_dir.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = result_dir / f"{hostname}_{timestamp}.json"
    out_path.write_text(
        json.dumps(
            {
                "context": {"public_ip": public_ip, "hostname": hostname, **ec2_ctx},
                "attempts": [asdict(a) for a in attempts],
                "success_rates": success_rates,
            },
            indent=2,
        )
    )
    console.print(f"\n[dim]Results written to {out_path}[/dim]")

    non_browser_ok = any(success_rates.get(s, 0) >= 70 for s in strategies if s in NON_BROWSER_STRATEGIES)
    if non_browser_ok:
        return 0
    playwright_ok = any(
        success_rates.get(s, 0) >= 70 for s in strategies if s not in NON_BROWSER_STRATEGIES
    )
    return 2 if playwright_ok else 1


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe whether Amazon blocks traffic from this host, per strategy.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--queries", nargs="+", default=DEFAULT_QUERIES)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--delay", type=float, default=8.0, help="Seconds between requests (plus jitter).")
    parser.add_argument(
        "--strategies",
        nargs="+",
        choices=ALL_STRATEGIES,
        default=ALL_STRATEGIES,
    )
    parser.add_argument(
        "--no-cache",
        dest="no_cache",
        action="store_true",
        default=True,
        help="Always on — this probe never reads AmazonScraper's 4h FileCache. "
        "Flag kept for explicitness; there is no way to turn it off.",
    )
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    result_dir = Path(__file__).resolve().parent.parent / "probe_results"
    return asyncio.run(run_probe(args.queries, args.rounds, args.delay, args.strategies, result_dir))


if __name__ == "__main__":
    sys.exit(main())
