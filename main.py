import asyncio
import pandas as pd
from datetime import datetime
import logging
from typing import List, Dict, Any

from sqlalchemy import select
from database import init_db, session_scope, TrackedJob, ScrapeHistory, ScrapedProduct

from rich.prompt import Prompt, IntPrompt
from rich.table import Table

from debug_utils import (
    check_dependencies,
    setup_logging,
    handle_critical_error,
    print_header,
    console,
    Progress,
    SpinnerColumn,
    TextColumn,
    Panel
)

try:
    check_dependencies()
    from core_engine import execute_scrape
except ImportError as e:
    console.print(
        Panel(
            f"[bold red]Initialization Error![/bold red]\n\n"
            f"An essential library might be missing or there's an import issue.\n"
            f"Please check the error below and your environment.\n\n"
            f"[italic white]Error: {e}[/italic white]",
            title="[bold yellow]Setup Failure[/bold yellow]",
            border_style="red",
        )
    )
    exit(1)

setup_logging()
logger = logging.getLogger(__name__)


def get_user_input() -> Dict[str, Any]:
    """Gets all necessary input from the user."""
    choice_to_platforms = {
        "1": ["amazon"],
        "2": ["mercadolibre"],
        "3": ["amazon", "mercadolibre"],
    }
    console.print("[bold]Select a platform to scrape:[/bold]")
    console.print("  [cyan]1[/cyan]. Amazon")
    console.print("  [cyan]2[/cyan]. MercadoLibre")
    console.print("  [cyan]3[/cyan]. Both")

    choice = Prompt.ask(
        "[bold]Enter your choice[/bold]", choices=["1", "2", "3"], default="3"
    )
    query = Prompt.ask("[bold yellow]What product are you looking for?[/bold yellow]")
    pages = IntPrompt.ask(
        "[bold yellow]How many pages per platform? (0 = all available)[/bold yellow]", default=1
    )
    pages = 0 if pages == 0 else min(pages, 10)

    include_ads_str = Prompt.ask(
        "[bold yellow]Include sponsored/advertised products?[/bold yellow]",
        choices=["y", "n"],
        default="y",
    )
    include_ads = include_ads_str == "y"

    return {
        "platforms": choice_to_platforms[choice],
        "query": query,
        "pages": pages,
        "include_ads": include_ads,
    }


def display_results_table(products: List[Dict[str, Any]]) -> None:
    """Displays scraped results in a rich Table."""
    if not products:
        console.print("\n[bold red]No products were found.[/bold red]")
        console.print(
            "[yellow]Check 'scraper.log' for a detailed step-by-step report and the 'debug_pages' folder for any saved HTML files.[/yellow]"
        )
        return

    table = Table(
        title=f"Found {len(products)} Unique Products",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("Source", style="dim", width=12)
    table.add_column("Product Title", style="cyan", no_wrap=False, max_width=50)
    table.add_column("Price", justify="right", style="green")
    table.add_column("Rating", justify="center")
    table.add_column("Reviews", justify="right")

    for product in sorted(products, key=lambda p: p.get("price") or float("inf"))[:30]:
        price_str = (
            f"{product.get('currency', '$')}{product.get('price'):,.2f}"
            if product.get("price") is not None
            else "[dim]N/A[/dim]"
        )
        rating_str = (
            str(product.get("rating"))
            if product.get("rating")
            else "[dim]N/A[/dim]"
        )
        reviews_str = (
            f"{product.get('review_count'):,}"
            if product.get("review_count") is not None
            else "[dim]N/A[/dim]"
        )

        table.add_row(
            product.get("source", "N/A"),
            product.get("title", "N/A"),
            price_str,
            rating_str,
            reviews_str,
        )
    console.print(table)


def export_to_excel(products: List[Dict[str, Any]], query: str) -> None:
    """Exports data to a formatted Excel file."""
    if not products:
        return

    df = pd.DataFrame(products)
    filename = f"scraped_data_{query.replace(' ', '_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

    try:
        with pd.ExcelWriter(filename, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Products")
            worksheet = writer.sheets["Products"]
            for column in worksheet.columns:
                max_length = max(len(str(cell.value)) for cell in column if cell.value)
                worksheet.column_dimensions[
                    column[0].column_letter
                ].width = max(20, max_length + 2)
        console.print(
            f"\n[bold green]Data successfully exported to [underline]{filename}[/underline][/bold green]"
        )
    except Exception as e:
        console.print(f"[bold red]Error exporting to Excel: {e}[/bold red]")


def smart_deduplicate(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deduplicates a list of products using a unique ID."""
    unique_products = {}
    for product in products:
        key = product.get("id")
        if key and key not in unique_products:
            unique_products[key] = product
    return list(unique_products.values())


async def export_history() -> None:
    """Query all tracked jobs from the local DB and export chosen job's history to Excel."""
    await init_db()  # ensure scraped_products table exists before querying
    async with session_scope() as session:
        result = await session.execute(
            select(TrackedJob).order_by(TrackedJob.created_at.desc())
        )
        jobs = result.scalars().all()

    if not jobs:
        console.print("\n[bold red]No tracked jobs found in the database.[/bold red]")
        console.print("[yellow]Run the Telegram bot and create a /track job first.[/yellow]")
        return

    console.print("\n[bold]Available jobs:[/bold]")
    for job in jobs:
        status = "[green]active[/green]" if job.is_active else "[dim]ended[/dim]"
        platforms = ", ".join(p.capitalize() for p in job.get_platforms())
        console.print(
            f"  [cyan]{job.id}[/cyan]. {job.query[:40]} | {platforms} | {status}"
        )

    job_ids = [str(j.id) for j in jobs]
    chosen_id = int(Prompt.ask(
        "[bold yellow]Enter Job ID to export[/bold yellow]", choices=job_ids
    ))

    async with session_scope() as session:
        j_result = await session.execute(
            select(TrackedJob).where(TrackedJob.id == chosen_id)
        )
        job = j_result.scalar_one()

        p_result = await session.execute(
            select(ScrapedProduct, ScrapeHistory.timestamp)
            .join(ScrapeHistory, ScrapedProduct.history_id == ScrapeHistory.id)
            .where(ScrapeHistory.job_id == chosen_id)
            .order_by(ScrapeHistory.timestamp.asc(), ScrapedProduct.id.asc())
        )
        rows = p_result.all()

    if not rows:
        console.print(f"\n[bold red]Job #{chosen_id} has no recorded products yet.[/bold red]")
        return

    records = [
        {
            "scraped_at": ts.strftime("%m/%d/%Y %H:%M") if ts else "",
            "id": p.product_id,
            "title": p.title,
            "price": p.price,
            "url": p.url,
            "source": p.source,
            "currency": p.currency,
            "rating": p.rating,
            "review_count": p.review_count,
        }
        for p, ts in rows
    ]
    df = pd.DataFrame(records)

    filename = (
        f"history_job{chosen_id}_{job.query[:20].replace(' ', '_')}"
        f"_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    )

    try:
        with pd.ExcelWriter(filename, engine="openpyxl") as writer:
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

        console.print(
            f"\n[bold green]Exported {len(rows)} product(s) to "
            f"[underline]{filename}[/underline][/bold green]"
        )
    except Exception as e:
        console.print(f"[bold red]Export failed: {e}[/bold red]")


async def trigger_job_scrape() -> None:
    """Manually run a scrape for a tracked job and persist results to the database."""
    await init_db()

    async with session_scope() as session:
        res = await session.execute(
            select(TrackedJob).where(TrackedJob.is_active == True).order_by(TrackedJob.id.desc())
        )
        jobs = res.scalars().all()

    if not jobs:
        console.print("\n[bold red]No active tracked jobs found.[/bold red]")
        return

    console.print("\n[bold]Active jobs:[/bold]")
    for job in jobs:
        platforms = ", ".join(p.capitalize() for p in job.get_platforms())
        console.print(f"  [cyan]{job.id}[/cyan]. {job.query[:40]} | {platforms}")

    job_ids = [str(j.id) for j in jobs]
    chosen_id = int(Prompt.ask("[bold yellow]Enter Job ID to scrape[/bold yellow]", choices=job_ids))

    async with session_scope() as session:
        j_res = await session.execute(select(TrackedJob).where(TrackedJob.id == chosen_id))
        job = j_res.scalar_one()
        platforms = job.get_platforms()
        query = job.query
        pages = job.pages_per_run
        include_ads = job.include_ads

    async with session_scope() as session:
        before_res = await session.execute(
            select(ScrapedProduct)
            .join(ScrapeHistory, ScrapedProduct.history_id == ScrapeHistory.id)
            .where(ScrapeHistory.job_id == chosen_id)
        )
        count_before = len(before_res.scalars().all())
    console.print(f"\nProducts in DB before: [bold]{count_before}[/bold]")

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True) as progress:
        progress.add_task(f"[bold]Scraping '{query}' on {', '.join(platforms)}...[/bold]", total=None)
        scrape_result = await execute_scrape(
            query=query,
            platforms=platforms,
            pages=pages,
            include_ads=include_ads,
        )

    if scrape_result["errors"]:
        for err in scrape_result["errors"]:
            console.print(f"[bold red]Scraper error:[/bold red] {err}")

    async with session_scope() as session:
        history = ScrapeHistory(
            job_id=chosen_id,
            total_found=scrape_result["total_found"],
            lowest_price=scrape_result["lowest_price"],
        )
        session.add(history)
        await session.flush()
        for p in scrape_result.get("products", []):
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

    async with session_scope() as session:
        after_res = await session.execute(
            select(ScrapedProduct)
            .join(ScrapeHistory, ScrapedProduct.history_id == ScrapeHistory.id)
            .where(ScrapeHistory.job_id == chosen_id)
        )
        count_after = len(after_res.scalars().all())

    console.print(f"Products found this run: [bold]{scrape_result['total_found']}[/bold]")
    console.print(f"Products in DB after:   [bold]{count_after}[/bold]  ([green]+{count_after - count_before}[/green] new)")


async def main_logic() -> None:
    """Main async function to orchestrate the scraping process."""
    print_header()

    console.print("[bold]What would you like to do?[/bold]")
    console.print("  [cyan]1[/cyan]. Search products")
    console.print("  [cyan]2[/cyan]. Export scrape history to Excel")
    console.print("  [cyan]3[/cyan]. Run test scrape for a tracked job")

    mode = Prompt.ask("[bold]Enter your choice[/bold]", choices=["1", "2", "3"], default="1")

    if mode == "2":
        await export_history()
        return

    if mode == "3":
        await trigger_job_scrape()
        return

    user_input = get_user_input()

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True
    ) as progress:
        progress.add_task("[bold]Scraping Platforms...[/bold]", total=None)
        result = await execute_scrape(
            query=user_input["query"],
            platforms=user_input["platforms"],
            pages=user_input["pages"],
            include_ads=user_input["include_ads"],
        )

    final_products = result["products"]

    if result["errors"]:
        for err in result["errors"]:
            console.print(f"[bold red]Error:[/bold red] {err}")

    display_results_table(final_products)

    if (
        final_products
        and Prompt.ask(
            "\n[bold]Export results to Excel?[/bold]", choices=["y", "n"], default="y"
        )
        == "y"
    ):
        export_to_excel(final_products, user_input["query"])


if __name__ == "__main__":
    try:
        asyncio.run(main_logic())
    except KeyboardInterrupt:
        console.print("\n[bold red]Scraping cancelled by user.[/bold red]")
    except Exception as e:
        handle_critical_error(e)