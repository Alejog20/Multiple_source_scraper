import asyncio
import hashlib
import json
import logging
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional, Callable

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

# --- Dependency Management ---
try:
    from importlib import metadata
except ImportError:
    # Python < 3.8
    import importlib_metadata as metadata

console = Console()

# A list of required packages and their installation names
REQUIRED_PACKAGES = {
    "pandas": "pandas",
    "openpyxl": "openpyxl",
    "rich": "rich",
    "httpx": "httpx[http2]",
    "selectolax": "selectolax",
    "playwright": "playwright",
}

# --- Enhanced User-Agent Pools ---
DESKTOP_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
]
MOBILE_USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; SM-G998B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
]


def get_random_user_agent(is_mobile: bool = False) -> str:
    return random.choice(MOBILE_USER_AGENTS if is_mobile else DESKTOP_USER_AGENTS)


def get_realistic_headers(is_mobile: bool = False) -> Dict[str, str]:
    """Generate realistic browser headers to avoid detection."""
    user_agent = get_random_user_agent(is_mobile)

    base_headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }

    if "Chrome" in user_agent:
        chrome_version = "130" if "130.0.0.0" in user_agent else "129"
        base_headers.update({
            "sec-ch-ua": f'"Chromium";v="{chrome_version}", "Google Chrome";v="{chrome_version}", "Not?A_Brand";v="99"',
            "sec-ch-ua-mobile": "?1" if is_mobile else "?0",
            "sec-ch-ua-platform": '"Android"' if is_mobile and "Android" in user_agent else '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        })

    return base_headers


# --- Async Retry Mechanism ---
async def retry_with_backoff(
    func: Callable,
    max_retries: int = 3,
    base_delay: float = 1.0,
    backoff_factor: float = 2.0,
    jitter: bool = True
) -> Any:
    """Retry a function with exponential backoff.

    Args:
        func: Async function to retry
        max_retries: Maximum number of retry attempts
        base_delay: Initial delay in seconds
        backoff_factor: Multiplier for delay between retries
        jitter: Add random jitter to delay

    Returns:
        Result of the function or None if all retries failed
    """
    last_exception = None

    for attempt in range(max_retries + 1):
        try:
            result = await func()
            return result
        except Exception as e:
            last_exception = e

            if attempt == max_retries:
                logging.error(f"All {max_retries} retry attempts failed. Last error: {e}")
                break

            delay = base_delay * (backoff_factor ** attempt)
            if jitter:
                delay += random.uniform(0, delay * 0.1)

            logging.warning(f"Attempt {attempt + 1} failed: {e}. Retrying in {delay:.2f}s...")
            await asyncio.sleep(delay)

    return None


# --- Data Validation ---
def validate_product_data(product: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and clean product data.

    Args:
        product: Raw product dictionary

    Returns:
        Cleaned and validated product dictionary
    """
    cleaned = {}

    # Validate ID
    if product.get("id") and str(product["id"]).strip():
        cleaned["id"] = str(product["id"]).strip()
    else:
        return None  # Invalid without ID

    # Validate title
    title = product.get("title")
    if title and isinstance(title, str) and title.strip():
        cleaned["title"] = title.strip()[:200]  # Truncate if too long
    else:
        return None  # Invalid without title

    # Validate price
    price = product.get("price")
    if price is not None:
        try:
            price_float = float(price)
            cleaned["price"] = price_float if price_float >= 0 else None  # Price can't be negative
        except (ValueError, TypeError):
            cleaned["price"] = None
    else:
        cleaned["price"] = None

    # Validate URL
    url = product.get("url")
    if url and isinstance(url, str) and url.strip():
        url = url.strip()
        if url.startswith(("http://", "https://")):
            cleaned["url"] = url
        else:
            cleaned["url"] = None
    else:
        cleaned["url"] = None

    # Copy other fields as-is after basic validation
    for field in ["source", "currency", "rating", "review_count"]:
        value = product.get(field)
        if field == "rating" and value is not None:
            try:
                rating_float = float(value)
                if 0 <= rating_float <= 5:  # Assuming 5-star scale
                    cleaned[field] = rating_float
                else:
                    cleaned[field] = None
            except (ValueError, TypeError):
                cleaned[field] = None
        elif field == "review_count" and value is not None:
            try:
                count_int = int(value)
                if count_int >= 0:
                    cleaned[field] = count_int
                else:
                    cleaned[field] = None
            except (ValueError, TypeError):
                cleaned[field] = None
        else:
            cleaned[field] = value

    return cleaned


# --- Caching System ---
class FileCache:
    def __init__(self, cache_dir: str = ".cache", max_age_hours: int = 4):
        self.cache_dir = Path(cache_dir)
        self.max_age = timedelta(hours=max_age_hours)
        self.cache_dir.mkdir(exist_ok=True)

    def _get_cache_path(self, url: str) -> Path:
        url_hash = hashlib.md5(url.encode()).hexdigest()
        return self.cache_dir / f"{url_hash}.json"

    def get(self, url: str) -> str | None:
        path = self._get_cache_path(url)
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f: data = json.load(f)
                cached_time = datetime.fromisoformat(data["timestamp"])
                if datetime.utcnow() - cached_time < self.max_age:
                    logging.info(f"[CACHE] Hit for URL: {url[:80]}...")
                    return data["html"]
            except (json.JSONDecodeError, KeyError):
                logging.warning(f"[CACHE] Corrupt cache file found: {path}")
        logging.info(f"[CACHE] Miss for URL: {url[:80]}...")
        return None

    def set(self, url: str, html: str) -> None:
        path = self._get_cache_path(url)
        data = {"timestamp": datetime.utcnow().isoformat(), "url": url, "html": html}
        with open(path, "w", encoding="utf-8") as f: json.dump(data, f)


# --- Debugging & Logging ---
def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - [%(module)s:%(lineno)d] - %(message)s",
        handlers=[logging.FileHandler("scraper.log", mode="w")],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def save_debug_html(html: str, prefix: str) -> None:
    debug_dir = Path("debug_pages")
    debug_dir.mkdir(exist_ok=True)
    filename = (
        debug_dir
        / f"debug_{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    )
    try:
        with open(filename, "w", encoding="utf-8") as f: f.write(html)
        logging.info(f"[DEBUG] Saved HTML to {filename}")
    except Exception as e:
        logging.error(f"[DEBUG] Could not save HTML file: {e}")


def log_html_snippet(
    logger: logging.Logger, platform: str, field: str, html: str
) -> None:
    snippet = html.replace("\n", " ").strip()[:500]
    logger.warning(
        f"[{platform}] PARSE FAIL: Could not extract '{field}'. Snippet: {snippet}"
    )


# --- UI & Error Handling ---
def print_header() -> None:
    console.print(
        Panel(
            "[bold cyan]Multi-Platform E-Commerce Scraper[/bold cyan]",
            title="[bold green]Version 5.0 (Definitive Edition)[/bold green]",
            subtitle="[italic]Professional Grade[/italic]",
            style="bold blue",
            width=80,
        )
    )
    console.print()


def check_dependencies() -> None:
    missing_packages = []
    for package, install_name in REQUIRED_PACKAGES.items():
        try:
            metadata.version(package)
        except metadata.PackageNotFoundError:
            missing_packages.append(install_name)

    if missing_packages:
        console.print(
            Panel(
                "[bold red]Error: Missing Required Libraries![/bold red]\n\n"
                "This tool requires some packages to be installed first.\n"
                "Please run the following command in your terminal:\n\n"
                f"[bold cyan]pip install {' '.join(missing_packages)}[/bold cyan]\n\n"
                "If you are using Playwright for the first time, you also need to install its browser drivers:\n"
                "[bold cyan]playwright install[/bold cyan]",
                title="[bold yellow]Dependency Check Failed[/bold yellow]",
                border_style="red",
            )
        )
        sys.exit(1)


def handle_critical_error(e: Exception) -> None:
    logger = logging.getLogger(__name__)
    logger.critical("A critical, unrecoverable error occurred.", exc_info=True)

    console.print(
        Panel(
            f"[bold red]A critical error occurred and the program had to stop.[/bold red]\n\n"
            f"[white]Error Details:[/white] [italic]{e}[/italic]\n\n"
            "A detailed error report has been saved to [bold cyan]scraper.log[/bold cyan].",
            title="[bold yellow]Execution Stopped[/bold yellow]",
            border_style="red",
        )
    )
    sys.exit(1)


# --- Error Classification ---
def is_retryable_error(status_code: Optional[int], exception: Optional[Exception] = None) -> bool:
    """Determine if an HTTP error or exception is retryable.

    Args:
        status_code: HTTP status code (if applicable)
        exception: Exception object (if applicable)

    Returns:
        True if the error should be retried, False otherwise
    """
    # Retryable HTTP status codes
    retryable_codes = {429, 502, 503, 504, 520, 521, 522, 523, 524}

    if status_code and status_code in retryable_codes:
        return True

    # Retryable exceptions
    if exception:
        retryable_exceptions = (
            "TimeoutError", "ConnectionError", "ConnectTimeout",
            "ReadTimeout", "PoolTimeout", "HTTPError"
        )
        return any(exc_type in str(type(exception)) for exc_type in retryable_exceptions)

    return False

