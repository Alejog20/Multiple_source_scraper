"""tests/test_amazon_scraper.py — Unit tests for AmazonScraper parsing logic
and resilience-funnel orchestration.

Parsing/validation and the funnel's fallback decision-making are pure,
offline-testable business logic and are covered here against inline HTML
fixtures. The methods that open a real httpx/Playwright connection
(_fetch_with_httpx, _fetch_with_playwright) are marked `# pragma: no cover`
in amazon_scraper.py — genuinely exercising them means either live network
calls or mocking so deeply the test stops verifying anything real (see
CLAUDE.md's coverage-scope decision). A couple of light smoke tests below
still cover their cache/retry wrapper logic without hitting the network.

Run with:
    uv run pytest tests/test_amazon_scraper.py -v
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from amazon_scraper import AmazonScraper


@pytest.fixture
def scraper(tmp_path):
    s = AmazonScraper()
    # Isolate FileCache to a temp dir so tests never read/write the repo's
    # real .cache directory (avoids cross-test / cross-run flakiness).
    from debug_utils import FileCache

    s.cache = FileCache(cache_dir=str(tmp_path / "cache"))
    return s


HAPPY_PATH_ITEM = """
<div data-component-type="s-search-result" data-asin="B0TESTASIN1">
    <h2><a href="/dp/B0TESTASIN1"><span>Great Widget 5000</span></a></h2>
    <span class="a-price"><span class="a-offscreen">$129.99</span></span>
    <span class="a-icon-alt">4.5 out of 5 stars</span>
    <span class="a-size-base" dir="auto">(1,234)</span>
    <a class="a-link-normal s-no-outline" href="/dp/B0TESTASIN1">link</a>
</div>
"""

WHOLE_FRACTION_PRICE_ITEM = """
<div data-component-type="s-search-result" data-asin="B0TESTASIN2">
    <h2><a href="/dp/B0TESTASIN2"><span>Another Widget</span></a></h2>
    <span class="a-price-whole">45,</span><span class="a-price-fraction">99</span>
    <a class="a-link-normal s-no-outline" href="/dp/B0TESTASIN2">link</a>
</div>
"""

NO_TITLE_ITEM = """
<div data-component-type="s-search-result" data-asin="B0TESTASIN3">
    <span class="a-price"><span class="a-offscreen">$10.00</span></span>
</div>
"""

NO_ASIN_ITEM = """
<div data-component-type="s-search-result">
    <h2><a href="/dp/XXX"><span>No ASIN Widget</span></a></h2>
</div>
"""

NO_PRICE_NO_URL_ITEM = """
<div data-component-type="s-search-result" data-asin="B0TESTASIN4">
    <h2><a><span>Priceless Widget</span></a></h2>
</div>
"""

PRICE_FALLBACK_ITEM = """
<div data-component-type="s-search-result" data-asin="B0PRICE1">
    <h2><a href="/dp/B0PRICE1"><span>Price Fallback Widget</span></a></h2>
    <span class="a-price-whole">abc,</span><span class="a-price-fraction">99</span>
    <span class="a-price"><span class="a-offscreen">$15.00</span></span>
</div>
"""

PRICE_NO_MATCH_ITEM = """
<div data-component-type="s-search-result" data-asin="B0PRICE2">
    <h2><a href="/dp/B0PRICE2"><span>No Price Widget</span></a></h2>
    <span class="a-price"><span class="a-offscreen">Out of Stock</span></span>
</div>
"""

RATING_FALLBACK_ITEM = """
<div data-component-type="s-search-result" data-asin="B0RATE1">
    <h2><a href="/dp/B0RATE1"><span>Rating Fallback Widget</span></a></h2>
    <span class="a-icon-alt">4.5 stars only</span>
</div>
"""

RATING_NO_MATCH_ITEM = """
<div data-component-type="s-search-result" data-asin="B0RATE2">
    <h2><a href="/dp/B0RATE2"><span>Unrated Widget</span></a></h2>
    <span class="a-icon-alt">no rating available</span>
</div>
"""

REVIEW_COMMA_ONLY_ITEM = """
<div data-component-type="s-search-result" data-asin="B0REV1">
    <h2><a href="/dp/B0REV1"><span>Comma Reviews Widget</span></a></h2>
    <span class="a-size-base" dir="auto">(,,,)</span>
</div>
"""

REVIEW_NO_DIGITS_ITEM = """
<div data-component-type="s-search-result" data-asin="B0REV2">
    <h2><a href="/dp/B0REV2"><span>No Digit Reviews Widget</span></a></h2>
    <span class="a-size-base" dir="auto">no reviews yet</span>
</div>
"""

SPONSORED_ITEM = """
<div data-component-type="s-search-result" data-asin="B0TESTASIN5">
    <h2><a href="/dp/B0TESTASIN5"><span>Sponsored Widget</span></a></h2>
    <span class="a-price"><span class="a-offscreen">$5.00</span></span>
    <span class="puis-sponsored-label-info-icon"></span>
</div>
"""

CAPTCHA_HTML = """
<html><body>
<form action="/errors/validateCaptcha"><input type="text" name="captcha"></form>
</body></html>
"""

NO_RESULTS_HTML = """
<html><body><h1>No results for "asdkjaslkdj12345"</h1></body></html>
"""

MINIMAL_CONTENT_HTML = "<html><body>Hi</body></html>"


def _wrap(*items: str) -> str:
    # Real (non-comment) filler text — selectolax's body.text() ignores HTML
    # comments, so padding must be visible text to clear the 100-char floor
    # _is_valid_page uses to reject near-empty/challenge pages.
    filler = "Padding text to satisfy the page validity length check. " * 3
    return f"<html><body><div style=\"display:none\">{filler}</div>{''.join(items)}</body></html>"


class TestIsValidPage:
    def test_captcha_page_is_invalid(self, scraper):
        _, is_valid = scraper._parse_html(CAPTCHA_HTML, is_mobile=False)
        assert is_valid is False

    def test_no_results_page_is_invalid(self, scraper):
        _, is_valid = scraper._parse_html(NO_RESULTS_HTML, is_mobile=False)
        assert is_valid is False

    def test_minimal_content_page_is_invalid(self, scraper):
        _, is_valid = scraper._parse_html(MINIMAL_CONTENT_HTML, is_mobile=False)
        assert is_valid is False

    def test_empty_html_is_invalid(self, scraper):
        _, is_valid = scraper._parse_html("", is_mobile=False)
        assert is_valid is False

    def test_valid_page_with_no_matching_items_returns_empty_list(self, scraper):
        products, is_valid = scraper._parse_html(_wrap(), is_mobile=False)
        assert is_valid is True
        assert products == []


class TestParseHtmlAndExtractProductInfo:
    def test_happy_path_extracts_all_fields(self, scraper):
        products, is_valid = scraper._parse_html(_wrap(HAPPY_PATH_ITEM), is_mobile=False)
        assert is_valid is True
        assert len(products) == 1
        p = products[0]
        assert p["id"] == "B0TESTASIN1"
        assert p["source"] == "Amazon"
        assert p["title"] == "Great Widget 5000"
        assert p["price"] == 129.99
        assert p["rating"] == 4.5
        assert p["review_count"] == 1234
        assert p["url"].endswith("/dp/B0TESTASIN1")
        assert p["is_ad"] is False

    def test_whole_fraction_price_format(self, scraper):
        products, _ = scraper._parse_html(_wrap(WHOLE_FRACTION_PRICE_ITEM), is_mobile=False)
        assert products[0]["price"] == 45.99

    def test_item_without_title_is_dropped(self, scraper):
        products, _ = scraper._parse_html(_wrap(NO_TITLE_ITEM), is_mobile=False)
        assert products == []

    def test_item_without_asin_is_dropped(self, scraper):
        products, _ = scraper._parse_html(_wrap(NO_ASIN_ITEM), is_mobile=False)
        assert products == []

    def test_item_without_price_or_explicit_url_falls_back_to_dp_url(self, scraper):
        products, _ = scraper._parse_html(_wrap(NO_PRICE_NO_URL_ITEM), is_mobile=False)
        assert len(products) == 1
        p = products[0]
        assert p["price"] is None
        assert p["url"] == "https://www.amazon.com/dp/B0TESTASIN4"

    def test_sponsored_item_is_tagged_is_ad(self, scraper):
        products, _ = scraper._parse_html(_wrap(SPONSORED_ITEM), is_mobile=False)
        assert products[0]["is_ad"] is True

    def test_second_container_selector_is_used_as_fallback(self, scraper):
        html = _wrap(
            '<div class="s-result-item" data-asin="B0FALLBACK1">'
            '<h2><a href="/dp/B0FALLBACK1"><span>Fallback Widget</span></a></h2>'
            "</div>"
        )
        products, is_valid = scraper._parse_html(html, is_mobile=False)
        assert is_valid is True
        assert len(products) == 1
        assert products[0]["id"] == "B0FALLBACK1"

    def test_price_falls_back_to_offscreen_after_invalid_whole_fraction(self, scraper):
        products, _ = scraper._parse_html(_wrap(PRICE_FALLBACK_ITEM), is_mobile=False)
        assert products[0]["price"] == 15.00

    def test_price_stays_none_when_no_selector_yields_a_number(self, scraper):
        products, _ = scraper._parse_html(_wrap(PRICE_NO_MATCH_ITEM), is_mobile=False)
        assert products[0]["price"] is None

    def test_rating_falls_back_to_bare_number_regex(self, scraper):
        products, _ = scraper._parse_html(_wrap(RATING_FALLBACK_ITEM), is_mobile=False)
        assert products[0]["rating"] == 4.5

    def test_rating_stays_none_when_no_regex_matches(self, scraper):
        products, _ = scraper._parse_html(_wrap(RATING_NO_MATCH_ITEM), is_mobile=False)
        assert products[0]["rating"] is None

    def test_review_count_comma_only_text_handled_gracefully(self, scraper):
        products, _ = scraper._parse_html(_wrap(REVIEW_COMMA_ONLY_ITEM), is_mobile=False)
        assert products[0]["review_count"] is None

    def test_review_count_no_digits_stays_none(self, scraper):
        products, _ = scraper._parse_html(_wrap(REVIEW_NO_DIGITS_ITEM), is_mobile=False)
        assert products[0]["review_count"] is None

    def test_mobile_and_desktop_parsing_share_same_validity(self, scraper):
        html = _wrap(HAPPY_PATH_ITEM)
        _, desktop_valid = scraper._parse_html(html, is_mobile=False)
        _, mobile_valid = scraper._parse_html(html, is_mobile=True)
        assert desktop_valid == mobile_valid


class TestGetUrlAndHeaders:
    def test_get_url_desktop(self, scraper):
        url = scraper._get_url("wireless mouse", 2, is_mobile=False)
        assert url == "https://www.amazon.com/s?k=wireless+mouse&page=2"

    def test_get_url_mobile(self, scraper):
        url = scraper._get_url("wireless mouse", 1, is_mobile=True)
        assert url == "https://www.amazon.com/gp/aw/s?k=wireless+mouse&page=1"

    def test_get_headers_delegates_to_realistic_headers(self, scraper):
        with patch("amazon_scraper.get_realistic_headers", return_value={"X": "Y"}) as mock_headers:
            result = scraper._get_headers(is_mobile=True)
        mock_headers.assert_called_once_with(True)
        assert result == {"X": "Y"}


class TestDeduplicate:
    def test_deduplicate_keeps_first_by_id(self, scraper):
        products = [
            {"id": "a", "title": "First"},
            {"id": "b", "title": "B"},
            {"id": "a", "title": "Second"},
        ]
        result = scraper._deduplicate(products)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Resilience funnel orchestration (mocked fetch methods — no network)
# ---------------------------------------------------------------------------


class TestExecuteStrategyFunnel:
    async def test_desktop_success_short_circuits_funnel(self, scraper):
        with (
            patch.object(scraper, "_fetch_with_httpx", new_callable=AsyncMock) as mock_httpx,
            patch.object(scraper, "_fetch_with_playwright", new_callable=AsyncMock) as mock_pw,
        ):
            mock_httpx.return_value = _wrap(HAPPY_PATH_ITEM)
            result = await scraper._execute_strategy_funnel(client=MagicMock(), query="q", page_num=1)
        assert len(result) == 1
        mock_pw.assert_not_called()

    async def test_falls_back_to_mobile_then_playwright(self, scraper):
        with (
            patch.object(scraper, "_fetch_with_httpx", new_callable=AsyncMock) as mock_httpx,
            patch.object(scraper, "_fetch_with_playwright", new_callable=AsyncMock) as mock_pw,
        ):
            # Desktop HTTPX returns invalid/empty content, mobile HTTPX too,
            # Playwright finally succeeds.
            mock_httpx.side_effect = [MINIMAL_CONTENT_HTML, MINIMAL_CONTENT_HTML]
            mock_pw.return_value = _wrap(HAPPY_PATH_ITEM)
            result = await scraper._execute_strategy_funnel(client=MagicMock(), query="q", page_num=1)
        assert len(result) == 1
        assert mock_httpx.await_count == 2
        mock_pw.assert_awaited_once()

    async def test_all_strategies_fail_returns_empty_list(self, scraper):
        with (
            patch.object(scraper, "_fetch_with_httpx", new_callable=AsyncMock) as mock_httpx,
            patch.object(scraper, "_fetch_with_playwright", new_callable=AsyncMock) as mock_pw,
        ):
            mock_httpx.return_value = None
            mock_pw.return_value = None
            result = await scraper._execute_strategy_funnel(client=MagicMock(), query="q", page_num=1)
        assert result == []

    async def test_mobile_success_short_circuits_before_playwright(self, scraper):
        with (
            patch.object(scraper, "_fetch_with_httpx", new_callable=AsyncMock) as mock_httpx,
            patch.object(scraper, "_fetch_with_playwright", new_callable=AsyncMock) as mock_pw,
        ):
            # Desktop invalid, mobile succeeds — Playwright must never run.
            mock_httpx.side_effect = [MINIMAL_CONTENT_HTML, _wrap(HAPPY_PATH_ITEM)]
            result = await scraper._execute_strategy_funnel(client=MagicMock(), query="q", page_num=1)
        assert len(result) == 1
        mock_pw.assert_not_called()

    async def test_playwright_returns_invalid_content_still_returns_empty(self, scraper):
        with (
            patch.object(scraper, "_fetch_with_httpx", new_callable=AsyncMock) as mock_httpx,
            patch.object(scraper, "_fetch_with_playwright", new_callable=AsyncMock) as mock_pw,
        ):
            mock_httpx.return_value = MINIMAL_CONTENT_HTML
            mock_pw.return_value = MINIMAL_CONTENT_HTML  # truthy but fails page validation
            result = await scraper._execute_strategy_funnel(client=MagicMock(), query="q", page_num=1)
        assert result == []


class TestSearchProductsPagination:
    async def test_stops_when_a_page_returns_no_products(self, scraper):
        with patch.object(scraper, "_execute_strategy_funnel", new_callable=AsyncMock) as mock_funnel:
            mock_funnel.side_effect = [
                [{"id": "a", "title": "A"}],
                [],
            ]
            products = await scraper.search_products(query="q", max_pages=5)
        assert len(products) == 1
        assert mock_funnel.await_count == 2  # stopped after the empty page

    async def test_stops_on_critical_failure_none(self, scraper):
        with patch.object(scraper, "_execute_strategy_funnel", new_callable=AsyncMock) as mock_funnel:
            mock_funnel.side_effect = [[{"id": "a", "title": "A"}], None]
            products = await scraper.search_products(query="q", max_pages=5)
        assert len(products) == 1
        assert mock_funnel.await_count == 2

    async def test_stops_on_first_page_with_zero_products(self, scraper):
        with patch.object(scraper, "_execute_strategy_funnel", new_callable=AsyncMock) as mock_funnel:
            mock_funnel.return_value = []
            products = await scraper.search_products(query="q", max_pages=5)
        assert products == []
        mock_funnel.assert_awaited_once()

    async def test_runs_all_pages_when_all_succeed(self, scraper):
        with patch.object(scraper, "_execute_strategy_funnel", new_callable=AsyncMock) as mock_funnel:
            mock_funnel.side_effect = [
                [{"id": "a", "title": "A"}],
                [{"id": "b", "title": "B"}],
            ]
            products = await scraper.search_products(query="q", max_pages=2)
        assert len(products) == 2
        assert mock_funnel.await_count == 2


# ---------------------------------------------------------------------------
# Light smoke coverage of the network-wrapper logic (cache short-circuit).
# These do not count toward the coverage gate (methods are pragma-excluded)
# but guard against regressions in the cache-hit fast path.
# ---------------------------------------------------------------------------


class TestFetchWithHttpxSmoke:
    async def test_cache_hit_skips_network_call(self, scraper):
        url = scraper._get_url("q", 1, is_mobile=False)
        scraper.cache.set(url, "<html>cached</html>")
        fake_client = AsyncMock(spec=httpx.AsyncClient)
        result = await scraper._fetch_with_httpx(fake_client, "q", 1, is_mobile=False)
        assert result == "<html>cached</html>"
        fake_client.get.assert_not_called()
