"""
scripts/manual_debug.py — Manual Scrape Debug Tool
====================================================
CLI tool for testing the scraping pipeline end-to-end without starting the
Telegram bot. Useful for verifying that scrapers work, inspecting raw
product data, and debugging price extraction.

No Telegram dependency — imports only database.py and core_engine.py.

Usage:
    uv run python scripts/manual_debug.py --query "iphone" --platforms amazon --pages 1
    uv run python scripts/manual_debug.py --query "laptop" --platforms amazon mercadolibre --pages 2
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Ensure the project root is on sys.path when running from scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.table import Table

from core_engine import execute_scrape
from database import TrackedJob, init_db, session_scope

console = Console()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manually trigger a scrape and display results.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--query",
        required=True,
        help="Search term to look up (e.g. 'Samsung Galaxy S24')",
    )
    parser.add_argument(
        "--platforms",
        nargs="+",
        choices=["amazon", "mercadolibre"],
        default=["amazon"],
        help="Platform(s) to search. Space-separated. Default: amazon",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=1,
        help="Number of pages to fetch per platform (default: 1)",
    )
    return parser


async def run_debug(query: str, platforms: list[str], pages: int) -> None:
    console.rule("[bold cyan]Manual Scrape Debug[/bold cyan]")
    console.print(
        f"[bold]Query:[/bold] {query}  "
        f"[bold]Platforms:[/bold] {', '.join(platforms)}  "
        f"[bold]Pages:[/bold] {pages}"
    )
    console.print()

    # Initialise DB (creates tables if they don't exist)
    await init_db()

    # Insert a dummy TrackedJob so there is a valid FK target for ScrapeHistory
    # if callers want to log history. This is optional — execute_scrape itself
    # doesn't need a job row.
    console.print("[dim]Calling execute_scrape…[/dim]")

    with console.status("[bold green]Scraping…[/bold green]", spinner="dots"):
        result = await execute_scrape(query=query, platforms=platforms, pages=pages)

    # --- Summary ---
    console.print()
    console.print(
        f"[bold green]Done.[/bold green]  "
        f"Total products found: [bold]{result['total_found']}[/bold]  |  "
        f"Lowest price: [bold]"
        + (f"${result['lowest_price']:.2f}" if result["lowest_price"] is not None else "N/A")
        + "[/bold]"
    )

    if result["errors"]:
        console.print()
        console.print("[bold red]Errors:[/bold red]")
        for err in result["errors"]:
            console.print(f"  [red]•[/red] {err}")

    if not result["products"]:
        console.print("\n[yellow]No products to display.[/yellow]")
        return

    # --- Results table ---
    table = Table(
        title=f"Products for '{query}'",
        show_lines=True,
        highlight=True,
    )
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("Source", style="cyan", width=14)
    table.add_column("Title", style="white", max_width=55, no_wrap=False)
    table.add_column("Price", style="green", width=12, justify="right")
    table.add_column("Rating", width=8, justify="center")
    table.add_column("URL", style="blue", max_width=40, no_wrap=True)

    for idx, product in enumerate(result["products"], start=1):
        price_cell = (
            f"${product['price']:.2f}" if product.get("price") is not None else "—"
        )
        rating_cell = (
            f"{product['rating']:.1f} ★" if product.get("rating") is not None else "—"
        )
        table.add_row(
            str(idx),
            product.get("source", "—"),
            product.get("title", "—"),
            price_cell,
            rating_cell,
            product.get("url") or "—",
        )

    console.print()
    console.print(table)


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    asyncio.run(run_debug(query=args.query, platforms=args.platforms, pages=args.pages))


if __name__ == "__main__":
    main()
