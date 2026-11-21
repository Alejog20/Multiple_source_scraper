"""tests/live/test_amazon_access.py — Opt-in live network sanity check.

Skipped by default and always in CI (the test workflow runs
`pytest -m "not live"`). Set RUN_LIVE=1 to actually hit amazon.com and
confirm the resilience funnel still returns products from this host.

This is a minimal "is anything working at all" check, not a measurement
tool — for a real per-strategy breakdown with success rates, use
scripts/probe_amazon.py.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE") != "1", reason="live network test; set RUN_LIVE=1 to run"
    ),
]


async def test_at_least_one_strategy_returns_products():
    from amazon_scraper import AmazonScraper

    scraper = AmazonScraper()
    products = await scraper.search_products(query="kindle paperwhite", max_pages=1)

    assert len(products) > 0
