"""tests/test_core_engine.py — Unit tests for the core_engine orchestration
layer not already covered by tests/test_scheduler.py (which owns
_deduplicate_products and _compute_lowest_price).

execute_scrape is the single sanctioned entry point into scraping (see
CLAUDE.md) — these tests mock AmazonScraper/MercadoLibreScraper at the class
level so the orchestration logic (dispatch, error aggregation, dedup,
ad-filtering, lowest-price) is verified without any network or Playwright
access.

Run with:
    uv run pytest tests/test_core_engine.py -v
"""

from unittest.mock import AsyncMock, patch

import pytest

import core_engine


def _product(id_, price=None, title="Product", is_ad=False, source="Amazon"):
    return {
        "id": id_,
        "source": source,
        "title": title,
        "url": f"https://example.com/{id_}",
        "price": price,
        "currency": "USD",
        "rating": None,
        "review_count": None,
        "is_ad": is_ad,
    }


# ---------------------------------------------------------------------------
# _filter_by_query_relevance
# ---------------------------------------------------------------------------


class TestFilterByQueryRelevance:
    def test_single_keyword_query_returns_products_unchanged(self):
        products = [_product("a", title="Anything at all")]
        result = core_engine._filter_by_query_relevance(products, "sigma")
        assert result == products

    def test_multi_keyword_keeps_only_matching_titles(self):
        products = [
            _product("a", title="Sigma 18-50mm Lens"),
            _product("b", title="Sigma bare brand result"),
        ]
        result = core_engine._filter_by_query_relevance(products, "Sigma 18-50")
        assert [p["id"] for p in result] == ["a"]

    def test_stop_words_are_ignored_as_keywords(self):
        products = [_product("a", title="The Big Camera Lens")]
        # "the" and "de" are stop-words — only "big"/"camera"/"lens" count.
        result = core_engine._filter_by_query_relevance(products, "the Camera Lens")
        assert result == products

    def test_product_with_none_title_is_excluded_when_keywords_dont_match(self):
        products = [{"id": "a", "title": None}]
        result = core_engine._filter_by_query_relevance(products, "camera lens")
        assert result == []

    def test_empty_query_returns_products_unchanged(self):
        products = [_product("a")]
        result = core_engine._filter_by_query_relevance(products, "")
        assert result == products


# ---------------------------------------------------------------------------
# execute_scrape orchestration
# ---------------------------------------------------------------------------


class TestExecuteScrape:
    async def test_dispatches_to_both_platforms_and_merges(self):
        with (
            patch("core_engine.AmazonScraper") as MockAmazon,
            patch("core_engine.MercadoLibreScraper") as MockML,
        ):
            MockAmazon.return_value.search_products = AsyncMock(
                return_value=[_product("a1", price=100.0, title="Camera Lens 50mm")]
            )
            MockML.return_value.search_products = AsyncMock(
                return_value=[_product("m1", price=50.0, title="Camera Lens 50mm", source="MercadoLibre")]
            )
            result = await core_engine.execute_scrape(
                query="camera lens 50mm",
                platforms=["amazon", "mercadolibre"],
                pages=2,
            )

        assert result["total_found"] == 2
        assert result["lowest_price"] == 50.0
        assert result["errors"] == []
        ids = {p["id"] for p in result["products"]}
        assert ids == {"a1", "m1"}

    async def test_unknown_platform_is_skipped_with_error(self):
        with patch("core_engine.AmazonScraper") as MockAmazon:
            MockAmazon.return_value.search_products = AsyncMock(return_value=[])
            result = await core_engine.execute_scrape(
                query="test", platforms=["ebay"], pages=1
            )
        assert result["products"] == []
        assert "Unknown platform 'ebay'" in result["errors"][0]

    async def test_platform_exception_is_caught_and_recorded(self):
        with (
            patch("core_engine.AmazonScraper") as MockAmazon,
            patch("core_engine.MercadoLibreScraper") as MockML,
        ):
            MockAmazon.return_value.search_products = AsyncMock(
                side_effect=RuntimeError("timed out")
            )
            MockML.return_value.search_products = AsyncMock(
                return_value=[_product("m1", price=20.0, title="Widget", source="MercadoLibre")]
            )
            result = await core_engine.execute_scrape(
                query="widget", platforms=["amazon", "mercadolibre"], pages=1
            )

        assert result["total_found"] == 1
        assert result["products"][0]["id"] == "m1"
        assert len(result["errors"]) == 1
        assert "Amazon scrape failed: timed out" in result["errors"][0]

    async def test_include_ads_false_filters_sponsored_products(self):
        with patch("core_engine.AmazonScraper") as MockAmazon:
            MockAmazon.return_value.search_products = AsyncMock(
                return_value=[
                    _product("a1", price=10.0, title="Widget Pro", is_ad=True),
                    _product("a2", price=20.0, title="Widget Pro", is_ad=False),
                ]
            )
            result = await core_engine.execute_scrape(
                query="widget pro",
                platforms=["amazon"],
                pages=1,
                include_ads=False,
            )
        assert [p["id"] for p in result["products"]] == ["a2"]

    async def test_pages_zero_uses_auto_page_ceiling(self):
        with patch("core_engine.AmazonScraper") as MockAmazon:
            mock_search = AsyncMock(return_value=[])
            MockAmazon.return_value.search_products = mock_search
            await core_engine.execute_scrape(query="x", platforms=["amazon"], pages=0)
        _, kwargs = mock_search.call_args
        assert kwargs["max_pages"] == core_engine._MAX_AUTO_PAGES

    async def test_dedup_across_platforms_keeps_first_occurrence(self):
        with (
            patch("core_engine.AmazonScraper") as MockAmazon,
            patch("core_engine.MercadoLibreScraper") as MockML,
        ):
            MockAmazon.return_value.search_products = AsyncMock(
                return_value=[_product("dup", price=99.0, title="Same Item Everywhere")]
            )
            MockML.return_value.search_products = AsyncMock(
                return_value=[_product("dup", price=1.0, title="Same Item Everywhere", source="MercadoLibre")]
            )
            result = await core_engine.execute_scrape(
                query="same item everywhere",
                platforms=["amazon", "mercadolibre"],
                pages=1,
            )
        assert result["total_found"] == 1
        assert result["products"][0]["source"] == "Amazon"

    async def test_no_products_no_valid_prices_returns_none_lowest_price(self):
        with patch("core_engine.AmazonScraper") as MockAmazon:
            MockAmazon.return_value.search_products = AsyncMock(return_value=[])
            result = await core_engine.execute_scrape(
                query="nothing", platforms=["amazon"], pages=1
            )
        assert result["lowest_price"] is None
        assert result["total_found"] == 0


class TestScrapePlatformDispatch:
    async def test_scrape_platform_amazon(self):
        with patch("core_engine.AmazonScraper") as MockAmazon:
            MockAmazon.return_value.search_products = AsyncMock(return_value=["x"])
            result = await core_engine._scrape_platform(
                platform="amazon", query="q", pages=1
            )
        assert result == ["x"]
        MockAmazon.return_value.search_products.assert_awaited_once_with(
            query="q", max_pages=1
        )

    async def test_scrape_platform_mercadolibre(self):
        with patch("core_engine.MercadoLibreScraper") as MockML:
            MockML.return_value.search_products = AsyncMock(return_value=["y"])
            result = await core_engine._scrape_platform(
                platform="mercadolibre", query="q", pages=3
            )
        assert result == ["y"]
        MockML.assert_called_once_with(country_code="co")

    async def test_scrape_platform_unsupported_raises(self):
        with pytest.raises(ValueError, match="Unsupported platform"):
            await core_engine._scrape_platform(platform="ebay", query="q", pages=1)
