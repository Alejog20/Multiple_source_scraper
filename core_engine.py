"""
core_engine.py — Business Logic Layer
======================================
The "brain" of the scraper service. This module owns the orchestration of
scrape runs: which platforms to call, in what order, how to merge results,
and how to compute summary statistics. It contains zero Telegram-specific or
CLI-specific code so it can be called from the bot scheduler, the CLI entry
point (main.py), and integration tests identically.

Concurrency design — why scrapers run sequentially:
    Both AmazonScraper and MercadoLibreScraper use Playwright as a last-resort
    fallback strategy. Each page fetch that reaches the Playwright path creates
    a full Chromium browser instance and destroys it after the page is fetched.
    If both scrapers were run concurrently via ``asyncio.gather``, their
    Playwright fallbacks could fire simultaneously, creating two (or more)
    browser instances in memory at the same time.

    On a typical low-cost VPS (512 MB–1 GB RAM), a single headless Chromium
    process consumes 150–300 MB. Two concurrent instances can easily trigger
    OOM (Out-Of-Memory) kills, crashing the entire bot process and losing all
    in-flight jobs.

    The solution has two layers:
    1. **Sequential execution**: scrapers for each platform run one after the
       other inside ``execute_scrape``. This trades 30–60 s of extra wall-clock
       time for guaranteed peak-memory safety.
    2. **Module-level Semaphore**: ``_browser_semaphore`` (capacity 1) ensures
       that even if the bot's JobQueue fires multiple scheduled jobs at the
       exact same second (e.g., two users both scheduled at 08:00), only one
       ``execute_scrape`` call can run at a time. Subsequent calls queue behind
       it rather than spawning concurrent browser instances.

    This approach is intentionally conservative. If server RAM is upgraded in
    the future, the semaphore capacity can be raised (e.g., ``Semaphore(2)``)
    and the sequential loop can be changed to ``asyncio.gather`` per platform
    — all changes in one place.
"""

import asyncio
import logging
from typing import Any, TypedDict

from amazon_scraper import AmazonScraper
from debug_utils import setup_logging, validate_product_data
from mercadolibre_scraper import MercadoLibreScraper

# ---------------------------------------------------------------------------
# Module-level logging
# ---------------------------------------------------------------------------

setup_logging()
_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level Semaphore — shared across ALL callers in this process
# ---------------------------------------------------------------------------

_browser_semaphore = asyncio.Semaphore(1)
"""Guards concurrent access to scrape execution.

Set to capacity 1 so only one ``execute_scrape`` call runs at any given time.
This is intentionally module-level (not created inside ``execute_scrape``)
so the single Semaphore instance is shared across every coroutine that imports
this module. A per-call Semaphore would be useless — it would never block
because each call would own its own lock.

To allow higher concurrency in the future (on a server with more RAM), simply
increase the capacity::

    _browser_semaphore = asyncio.Semaphore(2)  # allows 2 concurrent scrapes
"""

# Supported platform identifiers. Used for validation and dispatch.
_SUPPORTED_PLATFORMS: frozenset[str] = frozenset({"amazon", "mercadolibre"})

_MAX_AUTO_PAGES: int = 50
"""Pages ceiling used when the caller requests 'all pages' (pages=0).
With the stop-on-empty fix in each scraper, the actual number of pages
fetched will always be much less; 50 is just a safety ceiling."""

# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------


class ScrapeResult(TypedDict):
    """Structured return type for :func:`execute_scrape`.

    Using a TypedDict instead of a plain ``dict`` gives callers static type
    checking on result keys and makes the contract explicit in IDE tooling.

    Attributes:
        products: Deduplicated, validated list of product dicts from all
            requested platforms. Each dict has keys: ``id``, ``source``,
            ``title``, ``url``, ``price``, ``currency``, ``rating``,
            ``review_count``.
        lowest_price: The minimum numeric price found across all products, or
            ``None`` if no product had a valid (non-None) price. This is the
            headline figure sent to Telegram users.
        total_found: Number of products in ``products`` after deduplication
            and validation. May be 0 if all scrapers failed or returned empty.
        errors: List of human-readable error messages, one per platform that
            raised an exception. Empty list means all platforms succeeded.
            The caller (bot scheduler) should forward these to the user while
            keeping the scheduled job active for the next run.
    """

    products: list[dict[str, Any]]
    lowest_price: float | None
    total_found: int
    errors: list[str]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def execute_scrape(
    query: str,
    platforms: list[str],
    pages: int,
    include_ads: bool = True,
) -> ScrapeResult:
    """Run a price-search across the requested platforms and return merged results.

    This is the single entry point for all scraping activity in the service.
    It is designed to be called from the Telegram bot's scheduled task, the
    CLI ``main.py``, and from developer debug scripts without modification.

    Concurrency safety:
        Acquires ``_browser_semaphore`` before doing any work. If another
        ``execute_scrape`` call is already in progress, this coroutine will
        suspend here until the semaphore is released, preventing concurrent
        Playwright browser instances.

    Platform order:
        Platforms are executed sequentially in the order they appear in the
        ``platforms`` list. A failure on one platform is caught, logged, and
        recorded in ``errors`` — the remaining platforms still run. This means
        a partial result (e.g., MercadoLibre products only, because Amazon
        timed out) is always returned rather than an all-or-nothing failure.

    Args:
        query: The search term to look up (e.g., ``"Samsung Galaxy S24"``).
            Passed directly to each scraper's ``search_products`` method.
        platforms: List of platform identifiers to search. Valid values are
            ``"amazon"`` and ``"mercadolibre"``. Unknown values are logged and
            skipped. Order determines execution sequence.
        pages: Number of result pages to fetch per platform. Each platform
            interprets this differently (Amazon: web pages; MercadoLibre: API
            pages of 50 items each). Capped at 10 by callers to prevent
            runaway memory use. ``pages=0`` means auto (up to
            ``_MAX_AUTO_PAGES``).
        include_ads: When ``False``, removes tagged sponsored products from
            results after scraping.

    Returns:
        A :class:`ScrapeResult` TypedDict with keys ``products``,
        ``lowest_price``, ``total_found``, and ``errors``.

    Example::

        result = await execute_scrape(
            query="laptop gaming",
            platforms=["amazon", "mercadolibre"],
            pages=3,
        )
        if result["errors"]:
            print(f"Partial failure: {result['errors']}")
        print(f"Found {result['total_found']} products, "
              f"lowest price: {result['lowest_price']}")
    """
    all_products: list[dict[str, Any]] = []
    errors: list[str] = []

    effective_pages = _MAX_AUTO_PAGES if pages == 0 else pages

    _logger.info(
        "execute_scrape: query=%r platforms=%r pages=%d (effective=%d) include_ads=%r — waiting for semaphore",
        query,
        platforms,
        pages,
        effective_pages,
        include_ads,
    )

    # Acquire the semaphore. All subsequent work happens inside this block.
    # The `async with` releases the semaphore automatically on exit, even if
    # an unhandled exception propagates out of the loop below.
    async with _browser_semaphore:
        _logger.info(
            "execute_scrape: semaphore acquired — starting sequential platform execution"
        )

        # --- Sequential platform loop ---
        # See module docstring for the full explanation of why this is NOT
        # asyncio.gather. Short version: concurrent Playwright = OOM risk.
        for platform in platforms:
            platform_lower = platform.strip().lower()

            if platform_lower not in _SUPPORTED_PLATFORMS:
                _logger.warning(
                    "execute_scrape: unknown platform %r — skipping", platform
                )
                errors.append(f"Unknown platform '{platform}' was skipped.")
                continue

            try:
                products = await _scrape_platform(
                    platform=platform_lower,
                    query=query,
                    pages=effective_pages,
                )
                _logger.info(
                    "execute_scrape: platform=%r returned %d products",
                    platform_lower,
                    len(products),
                )
                all_products.extend(products)

            except Exception as exc:  # noqa: BLE001 — intentional broad catch
                # Catching broadly here is deliberate. A network timeout, a
                # site structure change, a Playwright crash — all of these
                # should be surfaced to the user as a notification, not as an
                # unhandled exception that kills the scheduler job. The job
                # must survive individual run failures.
                msg = f"{platform_lower.capitalize()} scrape failed: {exc!s}"
                _logger.warning(
                    "execute_scrape: platform=%r raised %s: %s",
                    platform_lower,
                    type(exc).__name__,
                    exc,
                )
                errors.append(msg)

    # Post-processing happens outside the semaphore — it's pure CPU work,
    # no browser or network I/O, so there's no reason to hold the lock.
    deduplicated = _deduplicate_products(all_products)
    validated = [p for p in (validate_product_data(p) for p in deduplicated) if p]
    validated = _filter_by_query_relevance(validated, query)

    if not include_ads:
        before_count = len(validated)
        validated = [p for p in validated if not p.get("is_ad", False)]
        ads_removed = before_count - len(validated)
        _logger.info("execute_scrape: ads_removed=%d", ads_removed)

    lowest_price = _compute_lowest_price(validated)

    _logger.info(
        "execute_scrape: done — total_found=%d, lowest_price=%s, errors=%d",
        len(validated),
        lowest_price,
        len(errors),
    )

    return ScrapeResult(
        products=validated,
        lowest_price=lowest_price,
        total_found=len(validated),
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


async def _scrape_platform(
    platform: str,
    query: str,
    pages: int,
) -> list[dict[str, Any]]:
    """Instantiate the correct scraper and run it for the given platform.

    Scrapers are instantiated fresh on each call rather than being cached as
    module-level singletons. This is intentional: both :class:`AmazonScraper`
    and :class:`MercadoLibreScraper` hold a :class:`~debug_utils.FileCache`
    instance that accumulates state. A fresh instance starts with a clean
    in-memory state while still benefiting from the on-disk file cache,
    avoiding stale state from previous runs leaking between scheduled jobs.

    Args:
        platform: One of ``"amazon"`` or ``"mercadolibre"``. Must already be
            normalised (lowercase, stripped) by the caller.
        query: The search term to pass to the scraper.
        pages: Number of pages to fetch.

    Returns:
        Raw product list from the scraper's ``search_products`` method.
        May be empty if the scraper found nothing or encountered errors it
        handled internally (e.g., all retries exhausted silently).

    Raises:
        Any exception raised by the scraper that was not caught internally.
        The caller (:func:`execute_scrape`) is responsible for catching these.
    """
    if platform == "amazon":
        scraper = AmazonScraper()
        return await scraper.search_products(query=query, max_pages=pages)

    if platform == "mercadolibre":
        # country_code defaults to "co" (Colombia). In a future version this
        # could be a per-job field stored in TrackedJob, allowing users to
        # choose their MercadoLibre country.
        scraper = MercadoLibreScraper(country_code="co")
        return await scraper.search_products(query=query, max_pages=pages)

    # This branch is unreachable if the caller validated the platform value,
    # but it is kept for defensive completeness.
    raise ValueError(f"Unsupported platform: {platform!r}")


def _deduplicate_products(
    products: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove duplicate products, keeping the first occurrence of each ID.

    Deduplication is necessary because a query that spans both Amazon and
    MercadoLibre could theoretically return the same product (matched by ID),
    and a single platform may return the same item on multiple pages before
    its own internal deduplication runs.

    Uses an insertion-order-preserving approach: build a dict keyed by product
    ID where the first occurrence wins. This is cleaner than a set-based
    approach because dict keys maintain insertion order in Python 3.7+.

    Args:
        products: Raw merged product list, potentially containing duplicates.

    Returns:
        Deduplicated list with original relative order preserved.
    """
    seen: dict[str, dict[str, Any]] = {}
    for product in products:
        product_id: str = product.get("id", "")
        if product_id and product_id not in seen:
            seen[product_id] = product
    return list(seen.values())


_RELEVANCE_STOP_WORDS: frozenset[str] = frozenset({
    "the", "a", "an", "and", "or", "for", "of", "in", "on", "at", "to",
    "with", "de", "la", "el", "en", "y", "con",
})


def _filter_by_query_relevance(
    products: list[dict[str, Any]],
    query: str,
) -> list[dict[str, Any]]:
    """Keep only products whose title contains all significant query keywords.

    Eliminates noise results that share a brand/model prefix but ignore the
    rest of the query (e.g. bare 'Sigma' results when searching 'Sigma 18-50 mm').

    Tokenisation: split on whitespace, lowercase, drop stop-words and
    single-character tokens. Each remaining keyword must appear as a
    case-insensitive substring of the product title.

    Substring matching (not word-boundary) is intentional: spec tokens like
    "18-50" appear embedded in compound words such as "18-50mm" and a
    word-boundary regex would miss them.

    Returns products unchanged if the query yields fewer than two meaningful
    keywords (single-token queries need no narrowing).
    """
    import re

    raw_tokens = re.split(r"[\s,;]+", query.strip())
    keywords = [
        t.lower()
        for t in raw_tokens
        if len(t) >= 2 and t.lower() not in _RELEVANCE_STOP_WORDS
    ]

    if len(keywords) < 2:
        return products

    kept, removed = [], 0
    for product in products:
        title = (product.get("title") or "").lower()
        if all(kw in title for kw in keywords):
            kept.append(product)
        else:
            removed += 1

    if removed:
        _logger.info(
            "filter_by_query_relevance: removed=%d, kept=%d, query=%r",
            removed,
            len(kept),
            query,
        )
    return kept


def _compute_lowest_price(
    products: list[dict[str, Any]],
) -> float | None:
    """Return the minimum price across all products, or None if unavailable.

    Prices can be ``None`` for several reasons: the product listing has no
    price displayed, the price parser failed to extract a valid number, or
    the product is out of stock. These are filtered out before computing the
    minimum so that a single unparseable price does not corrupt the result.

    Args:
        products: List of validated product dicts. Each dict has a ``"price"``
            key whose value is either a ``float`` or ``None``.

    Returns:
        The lowest numeric price as a ``float``, or ``None`` if no product
        in the list has a valid price.
    """
    prices: list[float] = [
        p["price"] for p in products if p.get("price") is not None
    ]
    return min(prices) if prices else None
