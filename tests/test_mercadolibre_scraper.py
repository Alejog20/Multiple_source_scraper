"""tests/test_mercadolibre_scraper.py — Unit tests for MercadoLibreScraper
parsing logic and resilience-funnel orchestration.

Mirrors tests/test_amazon_scraper.py's approach: parsing/validation and the
funnel's fallback decision-making are pure, offline-testable business logic
covered here against inline HTML/JSON fixtures. _fetch_with_api,
_fetch_with_curl and _fetch_with_playwright open real network/browser
connections and are marked `# pragma: no cover` in mercadolibre_scraper.py
per CLAUDE.md's coverage-scope decision.

Run with:
    uv run pytest tests/test_mercadolibre_scraper.py -v
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mercadolibre_scraper import MercadoLibreScraper


@pytest.fixture
def scraper(tmp_path):
    s = MercadoLibreScraper(country_code="co")
    from debug_utils import FileCache

    s.cache = FileCache(cache_dir=str(tmp_path / "cache"))
    return s


def _filler_div() -> str:
    text = "Padding text to satisfy the page validity length check. " * 3
    return f'<div style="display:none">{text}</div>'


def _wrap(*items: str) -> str:
    return f"<html><body>{_filler_div()}{''.join(items)}</body></html>"


# ---------------------------------------------------------------------------
# _parse_api_data
# ---------------------------------------------------------------------------


class TestParseApiData:
    def test_empty_results_returns_empty_list(self, scraper):
        assert scraper._parse_api_data({"results": []}) == []
        assert scraper._parse_api_data({}) == []

    def test_maps_fields_and_default_is_ad_false(self, scraper):
        data = {
            "results": [
                {
                    "id": "MCO1",
                    "title": "Widget",
                    "permalink": "https://mercadolibre.com.co/w",
                    "price": 1000,
                    "currency_id": "COP",
                }
            ]
        }
        products = scraper._parse_api_data(data)
        assert len(products) == 1
        p = products[0]
        assert p["id"] == "MCO1"
        assert p["source"] == "MercadoLibre"
        assert p["price"] == 1000
        assert p["currency"] == "COP"
        assert p["is_ad"] is False

    def test_position_type_advertising_tags_is_ad(self, scraper):
        data = {"results": [{"id": "MCO2", "position_type": "advertising"}]}
        products = scraper._parse_api_data(data)
        assert products[0]["is_ad"] is True

    def test_is_advertising_flag_tags_is_ad(self, scraper):
        data = {"results": [{"id": "MCO3", "is_advertising": True}]}
        products = scraper._parse_api_data(data)
        assert products[0]["is_ad"] is True


# ---------------------------------------------------------------------------
# _extract_product_from_json_data_item (used by the __NEXT_DATA__/NORDIC
# JSON parsing path)
# ---------------------------------------------------------------------------


def _json_item(product_id="MCO123", title="JSON Widget", price=99.9, currency="COP", url="/MCO123-widget"):
    components = []
    if title is not None:
        components.append({"type": "title", "title": {"text": title}})
    if price is not None:
        components.append(
            {
                "type": "price",
                "price": {"current_price": {"value": price, "currency": currency}},
            }
        )
    return {"metadata": {"id": product_id, "url": url}, "components": components}


class TestExtractProductFromJsonDataItem:
    def test_full_item_extracted(self, scraper):
        product = scraper._extract_product_from_json_data_item(_json_item())
        assert product["id"] == "MCO123"
        assert product["title"] == "JSON Widget"
        assert product["price"] == 99.9
        assert product["currency"] == "COP"

    def test_relative_slash_url_prefixed_with_base_url(self, scraper):
        item = _json_item(url="/MCO999-item")
        product = scraper._extract_product_from_json_data_item(item)
        assert product["url"] == "https://www.mercadolibre.com.co/MCO999-item"

    def test_www_url_prefixed_with_https(self, scraper):
        item = _json_item(url="www.mercadolibre.com.co/MCO888-item")
        product = scraper._extract_product_from_json_data_item(item)
        assert product["url"] == "https://www.mercadolibre.com.co/MCO888-item"

    def test_absolute_url_left_unchanged(self, scraper):
        item = _json_item(url="https://www.mercadolibre.com.co/MCO777-item")
        product = scraper._extract_product_from_json_data_item(item)
        assert product["url"] == "https://www.mercadolibre.com.co/MCO777-item"

    def test_click1_url_tags_is_ad(self, scraper):
        item = _json_item(url="https://click1.mercadolibre.com.co/track?x=1")
        product = scraper._extract_product_from_json_data_item(item)
        assert product["is_ad"] is True

    def test_unparseable_price_value_becomes_none(self, scraper):
        item = _json_item(price="not-a-number")
        product = scraper._extract_product_from_json_data_item(item)
        assert product["price"] is None

    def test_missing_price_component_leaves_price_none(self, scraper):
        item = _json_item(price=None)
        product = scraper._extract_product_from_json_data_item(item)
        assert product["price"] is None
        assert product["currency"] is None

    def test_missing_id_drops_product(self, scraper):
        item = _json_item(product_id=None)
        assert scraper._extract_product_from_json_data_item(item) is None

    def test_unrelated_component_type_is_ignored(self, scraper):
        item = _json_item()
        item["components"].append({"type": "shipping", "shipping": {"text": "Free"}})
        product = scraper._extract_product_from_json_data_item(item)
        assert product["title"] == "JSON Widget"

    def test_price_component_with_no_value_leaves_price_none(self, scraper):
        item = {
            "metadata": {"id": "MCO1", "url": "/x"},
            "components": [
                {"type": "title", "title": {"text": "No Price Value Widget"}},
                {"type": "price", "price": {"current_price": {}}},
            ],
        }
        product = scraper._extract_product_from_json_data_item(item)
        assert product["price"] is None


# ---------------------------------------------------------------------------
# _parse_html — JSON script-tag paths (__NEXT_DATA__ / __NORDIC_RENDERING_CTX__)
# ---------------------------------------------------------------------------


class TestParseHtmlJsonPaths:
    def test_next_data_props_pageprops_path(self, scraper):
        payload = {
            "props": {
                "pageProps": {"initialState": {"results": [_json_item(product_id="MCO1")]}}
            }
        }
        html = (
            f'<html><body><script id="___NEXT_DATA__">{json.dumps(payload)}</script>'
            f"</body></html>"
        )
        products, is_valid = scraper._parse_html(html)
        assert is_valid is True
        assert products[0]["id"] == "MCO1"

    def test_query_results_path(self, scraper):
        payload = {"query": {"results": [_json_item(product_id="MCO2")]}}
        html = f'<html><body><script id="___NEXT_DATA__">{json.dumps(payload)}</script></body></html>'
        products, is_valid = scraper._parse_html(html)
        assert is_valid is True
        assert products[0]["id"] == "MCO2"

    def test_nordic_ctx_appprops_path(self, scraper):
        payload = {
            "appProps": {
                "pageProps": {"initialState": {"results": [_json_item(product_id="MCO3")]}}
            }
        }
        script_body = f"_n.ctx.r={json.dumps(payload)};_n.ctx.r.assets.manifest=new Map([])"
        html = (
            f'<html><body><script id="__NORDIC_RENDERING_CTX__">{script_body}</script>'
            f"</body></html>"
        )
        products, is_valid = scraper._parse_html(html)
        assert is_valid is True
        assert products[0]["id"] == "MCO3"

    def test_nordic_ctx_missing_assignment_prefix_is_invalid(self, scraper):
        html = (
            '<html><body><script id="__NORDIC_RENDERING_CTX__">'
            "totally_unexpected_content()"
            "</script></body></html>"
        )
        products, is_valid = scraper._parse_html(html)
        assert products == []
        assert is_valid is False

    def test_item_that_fails_extraction_is_skipped_others_kept(self, scraper):
        payload = {
            "props": {
                "pageProps": {
                    "initialState": {
                        "results": [
                            _json_item(product_id=None),  # dropped — no id
                            _json_item(product_id="MCO9"),
                        ]
                    }
                }
            }
        }
        html = f'<html><body><script id="___NEXT_DATA__">{json.dumps(payload)}</script></body></html>'
        products, is_valid = scraper._parse_html(html)
        assert is_valid is True
        assert [p["id"] for p in products] == ["MCO9"]

    def test_polycard_wrapper_is_unwrapped(self, scraper):
        inner = _json_item(product_id="MCO4")
        payload = {
            "props": {
                "pageProps": {
                    "initialState": {
                        "results": [{"id": "POLYCARD", "polycard": inner}]
                    }
                }
            }
        }
        html = f'<html><body><script id="___NEXT_DATA__">{json.dumps(payload)}</script></body></html>'
        products, is_valid = scraper._parse_html(html)
        assert is_valid is True
        assert products[0]["id"] == "MCO4"

    def test_json_decode_error_is_invalid(self, scraper):
        html = '<html><body><script id="___NEXT_DATA__">{not valid json</script></body></html>'
        products, is_valid = scraper._parse_html(html)
        assert products == []
        assert is_valid is False

    def test_no_recognizable_results_key_is_invalid(self, scraper):
        payload = {"something": {"else": True}}
        html = f'<html><body><script id="___NEXT_DATA__">{json.dumps(payload)}</script></body></html>'
        products, is_valid = scraper._parse_html(html)
        assert products == []
        assert is_valid is False


# ---------------------------------------------------------------------------
# _is_valid_page (exercised through the HTML fallback path — no script tag)
# ---------------------------------------------------------------------------


class TestIsValidPageHtmlFallback:
    def test_empty_html_invalid(self, scraper):
        _, is_valid = scraper._parse_html("")
        assert is_valid is False

    def test_challenge_page_invalid(self, scraper):
        html = _wrap('<div class="spinner">verifyChallenge in progress</div>')
        _, is_valid = scraper._parse_html(html)
        assert is_valid is False

    def test_no_results_specific_message_invalid(self, scraper):
        html = (
            "<html><body>"
            + "Padding text to satisfy the page validity length check. " * 3
            + '<div class="ui-search-rescue__title">'
            "No hay publicaciones que coincidan con tu bÃºsqueda"
            "</div></body></html>"
        )
        _, is_valid = scraper._parse_html(html)
        assert is_valid is False

    def test_minimal_content_invalid(self, scraper):
        _, is_valid = scraper._parse_html("<html><body>hi</body></html>")
        assert is_valid is False

    def test_valid_page_but_no_containers_found_is_invalid(self, scraper):
        # _is_valid_page passes (long enough body, no challenge markers) but
        # none of the container selectors match anything.
        products, is_valid = scraper._parse_html(_wrap())
        assert is_valid is False
        assert products == []


# ---------------------------------------------------------------------------
# _extract_product_info_from_html (HTML fallback container parsing)
# ---------------------------------------------------------------------------


PRIMARY_ITEM = """
<li class="ui-search-layout__item" data-item-id="MCO100">
    <h3 class="poly-component__title-wrapper">
        <a class="poly-component__title" href="/MCO-100-primary-widget">Primary Widget</a>
    </h3>
    <div class="poly-component__price">
        <div class="poly-price__current">
            <span class="andes-money-amount__currency-symbol">$</span>
            <span class="andes-money-amount__fraction">1.500.000</span>
        </div>
    </div>
</li>
"""

IMG_TITLE_ITEM = """
<li class="ui-search-layout__item" data-item-id="MCO101">
    <img title="Img Title Widget" src="x.jpg">
    <a class="ui-search-link" href="/MLA101-img-widget">link</a>
</li>
"""

IMG_NO_TITLE_ATTR_THEN_H2_ITEM = """
<li class="ui-search-layout__item">
    <img src="x.jpg">
    <h2 class="ui-search-item__title"><a href="/MLA102-h2-widget">Legacy H2 Widget</a></h2>
</li>
"""

DIV_LEGACY_TITLE_ITEM = """
<li class="ui-search-layout__item">
    <div class="ui-search-item__title"><a href="/MLA103-div-widget">Legacy Div Widget</a></div>
</li>
"""

NO_TITLE_AT_ALL_ITEM = """
<li class="ui-search-layout__item" data-item-id="MCO104">
    <span>no title markup here</span>
</li>
"""

PRICE_FALLBACK_ITEM = """
<li class="ui-search-layout__item">
    <h3 class="poly-component__title-wrapper">
        <a class="poly-component__title" href="/MLA105-price-fallback">Price Fallback Widget</a>
    </h3>
    <span class="price-tag-fraction">45.000</span>
</li>
"""

PRICE_UNPARSEABLE_THEN_MISSING_ITEM = """
<li class="ui-search-layout__item">
    <h3 class="poly-component__title-wrapper">
        <a class="poly-component__title" href="/MLA106-no-price">No Price Widget</a>
    </h3>
    <div class="price">N/A</div>
</li>
"""

MCO_DASH_ID_ITEM = """
<li class="ui-search-layout__item">
    <h3 class="poly-component__title-wrapper">
        <a class="poly-component__title" href="/producto/MCO-123456789-dash-id">Dash Id Widget</a>
    </h3>
</li>
"""

MLA_PREFIX_ID_ITEM = """
<li class="ui-search-layout__item">
    <h3 class="poly-component__title-wrapper">
        <a class="poly-component__title" href="/MLA555666777-prefixed">Prefixed Id Widget</a>
    </h3>
</li>
"""

NINE_DIGIT_FALLBACK_ID_ITEM = """
<li class="ui-search-layout__item">
    <h3 class="poly-component__title-wrapper">
        <a class="poly-component__title" href="/catalog/987654321">Nine Digit Fallback Widget</a>
    </h3>
</li>
"""

THREE_LETTER_FALLBACK_ID_ITEM = """
<li class="ui-search-layout__item">
    <h3 class="poly-component__title-wrapper">
        <a class="poly-component__title" href="/catalog/ABC123">Three Letter Fallback Widget</a>
    </h3>
</li>
"""

NO_ID_MATCH_BUT_DATA_ITEM_ID = """
<li class="ui-search-layout__item" data-item-id="DATA-ID-1">
    <h3 class="poly-component__title-wrapper">
        <a class="poly-component__title" href="/catalog/no-id-here">Data Item Id Widget</a>
    </h3>
</li>
"""

NO_ID_NO_DATA_ITEM_GENERATES_HASH = """
<li class="ui-search-layout__item">
    <h3 class="poly-component__title-wrapper">
        <a class="poly-component__title" href="/catalog/">Hash Generated Widget</a>
    </h3>
</li>
"""

NO_URL_AT_ALL_ITEM = """
<li class="ui-search-layout__item" data-item-id="MCO200">
    <h3 class="poly-component__title-wrapper">
        <a class="poly-component__title">No Link Widget</a>
    </h3>
</li>
"""

CLICK1_AD_ITEM = """
<li class="ui-search-layout__item">
    <h3 class="poly-component__title-wrapper">
        <a class="poly-component__title" href="https://click1.mercadolibre.com.co/track?x=1">Ad Via Click1</a>
    </h3>
</li>
"""

IS_ADVERTISING_CLASS_ITEM = """
<li class="ui-search-layout__item is-advertising" data-item-id="MCO201">
    <h3 class="poly-component__title-wrapper">
        <a class="poly-component__title" href="/MCO201-ad-class">Ad Via Class</a>
    </h3>
</li>
"""

COP_CURRENCY_TEXT_ITEM = """
<li class="ui-search-layout__item" data-item-id="MCO202">
    <h3 class="poly-component__title-wrapper">
        <a class="poly-component__title" href="/MCO202-cop-currency">COP Currency Widget</a>
    </h3>
    <span class="price-tag-symbol">COP</span>
</li>
"""

DATA_ITEM_ID_CONTAINER_FALLBACK = """
<div data-item-id="MCO300">
    <h3 class="poly-component__title-wrapper">
        <a class="poly-component__title" href="/MCO300-container-fallback">Container Fallback Widget</a>
    </h3>
</div>
"""


class TestExtractProductInfoFromHtml:
    def test_primary_title_price_currency_extracted(self, scraper):
        products, is_valid = scraper._parse_html(_wrap(PRIMARY_ITEM))
        assert is_valid is True
        p = products[0]
        assert p["title"] == "Primary Widget"
        assert p["price"] == 1500000
        assert p["currency"] == "$"

    def test_img_title_fallback(self, scraper):
        products, _ = scraper._parse_html(_wrap(IMG_TITLE_ITEM))
        assert products[0]["title"] == "Img Title Widget"

    def test_img_without_title_attr_falls_through_to_h2_legacy(self, scraper):
        products, _ = scraper._parse_html(_wrap(IMG_NO_TITLE_ATTR_THEN_H2_ITEM))
        assert products[0]["title"] == "Legacy H2 Widget"

    def test_div_legacy_title_selector(self, scraper):
        products, _ = scraper._parse_html(_wrap(DIV_LEGACY_TITLE_ITEM))
        assert products[0]["title"] == "Legacy Div Widget"

    def test_item_with_no_title_is_dropped(self, scraper):
        products, _ = scraper._parse_html(_wrap(NO_TITLE_AT_ALL_ITEM))
        assert products == []

    def test_price_tag_fraction_selector_fallback(self, scraper):
        products, _ = scraper._parse_html(_wrap(PRICE_FALLBACK_ITEM))
        assert products[0]["price"] == 45000

    def test_unparseable_price_text_leaves_price_none(self, scraper):
        products, _ = scraper._parse_html(_wrap(PRICE_UNPARSEABLE_THEN_MISSING_ITEM))
        assert products[0]["price"] is None

    def test_product_id_mco_dash_pattern(self, scraper):
        products, _ = scraper._parse_html(_wrap(MCO_DASH_ID_ITEM))
        assert products[0]["id"] == "MCO-123456789"

    def test_product_id_mla_prefix_pattern(self, scraper):
        products, _ = scraper._parse_html(_wrap(MLA_PREFIX_ID_ITEM))
        assert products[0]["id"] == "MLA555666777"

    def test_product_id_nine_digit_fallback(self, scraper):
        products, _ = scraper._parse_html(_wrap(NINE_DIGIT_FALLBACK_ID_ITEM))
        assert products[0]["id"] == "987654321"

    def test_product_id_three_letter_fallback(self, scraper):
        products, _ = scraper._parse_html(_wrap(THREE_LETTER_FALLBACK_ID_ITEM))
        assert products[0]["id"] == "ABC123"

    def test_product_id_falls_back_to_data_item_id_attribute(self, scraper):
        products, _ = scraper._parse_html(_wrap(NO_ID_MATCH_BUT_DATA_ITEM_ID))
        assert products[0]["id"] == "DATA-ID-1"

    def test_product_id_falls_back_to_generated_hash(self, scraper):
        products, _ = scraper._parse_html(_wrap(NO_ID_NO_DATA_ITEM_GENERATES_HASH))
        assert products[0]["id"].startswith("ML_GEN_")

    def test_item_without_url_still_falls_back_to_generated_id(self, scraper):
        products, _ = scraper._parse_html(_wrap(NO_URL_AT_ALL_ITEM))
        assert products[0]["url"] is None
        assert products[0]["id"] == "MCO200"  # data-item-id present

    def test_click1_url_tags_is_ad(self, scraper):
        products, _ = scraper._parse_html(_wrap(CLICK1_AD_ITEM))
        assert products[0]["is_ad"] is True

    def test_is_advertising_class_tags_is_ad(self, scraper):
        products, _ = scraper._parse_html(_wrap(IS_ADVERTISING_CLASS_ITEM))
        assert products[0]["is_ad"] is True

    def test_cop_currency_text_detected(self, scraper):
        products, _ = scraper._parse_html(_wrap(COP_CURRENCY_TEXT_ITEM))
        assert products[0]["currency"] == "COP"

    def test_container_selector_fallback_to_data_item_id_div(self, scraper):
        # No li.ui-search-layout__item / div.ui-search-result__wrapper /
        # .poly-card / .ui-search-results__item present at all — forces the
        # loop down to the `div[data-item-id]` selector.
        products, is_valid = scraper._parse_html(_wrap(DATA_ITEM_ID_CONTAINER_FALLBACK))
        assert is_valid is True
        assert products[0]["id"] == "MCO300"


# ---------------------------------------------------------------------------
# _get_url / _get_headers / _deduplicate
# ---------------------------------------------------------------------------


class TestGetUrlHeadersDeduplicate:
    def test_get_url_first_page(self, scraper):
        assert scraper._get_url("gaming laptop", 1) == "https://listado.mercadolibre.com.co/gaming-laptop"

    def test_get_url_later_page_uses_offset(self, scraper):
        assert (
            scraper._get_url("gaming laptop", 2)
            == "https://listado.mercadolibre.com.co/gaming-laptop_Desde_51"
        )

    def test_get_headers_delegates(self, scraper):
        with patch("mercadolibre_scraper.get_realistic_headers", return_value={"X": "Y"}) as m:
            result = scraper._get_headers(is_mobile=True)
        m.assert_called_once_with(True)
        assert result == {"X": "Y"}

    def test_deduplicate_keeps_first_by_id(self, scraper):
        products = [{"id": "a", "title": "1"}, {"id": "a", "title": "2"}, {"id": "b", "title": "3"}]
        assert len(scraper._deduplicate(products)) == 2


# ---------------------------------------------------------------------------
# Resilience funnel orchestration (mocked fetch methods — no network)
# ---------------------------------------------------------------------------


class TestExecuteStrategyFunnel:
    async def test_api_success_short_circuits_funnel(self, scraper):
        with (
            patch.object(scraper, "_fetch_with_api", new_callable=AsyncMock) as mock_api,
            patch.object(scraper, "_fetch_with_curl", new_callable=AsyncMock) as mock_curl,
            patch.object(scraper, "_fetch_with_playwright", new_callable=AsyncMock) as mock_pw,
        ):
            mock_api.return_value = {"results": [{"id": "MCO1", "title": "T", "permalink": "https://x", "price": 1, "currency_id": "COP"}]}
            result = await scraper._execute_strategy_funnel(query="q", page_num=1)
        assert len(result) == 1
        mock_curl.assert_not_called()
        mock_pw.assert_not_called()

    async def test_api_empty_falls_back_to_curl_html(self, scraper):
        with (
            patch.object(scraper, "_fetch_with_api", new_callable=AsyncMock) as mock_api,
            patch.object(scraper, "_fetch_with_curl", new_callable=AsyncMock) as mock_curl,
            patch.object(scraper, "_fetch_with_playwright", new_callable=AsyncMock) as mock_pw,
        ):
            mock_api.return_value = {"results": []}
            mock_curl.return_value = _wrap(PRIMARY_ITEM)
            result = await scraper._execute_strategy_funnel(query="q", page_num=1)
        assert len(result) == 1
        mock_pw.assert_not_called()

    async def test_api_and_curl_fail_falls_back_to_playwright(self, scraper):
        with (
            patch.object(scraper, "_fetch_with_api", new_callable=AsyncMock) as mock_api,
            patch.object(scraper, "_fetch_with_curl", new_callable=AsyncMock) as mock_curl,
            patch.object(scraper, "_fetch_with_playwright", new_callable=AsyncMock) as mock_pw,
        ):
            mock_api.return_value = None
            mock_curl.return_value = None
            mock_pw.return_value = _wrap(PRIMARY_ITEM)
            result = await scraper._execute_strategy_funnel(query="q", page_num=1)
        assert len(result) == 1

    async def test_all_strategies_fail_returns_empty_list(self, scraper):
        with (
            patch.object(scraper, "_fetch_with_api", new_callable=AsyncMock) as mock_api,
            patch.object(scraper, "_fetch_with_curl", new_callable=AsyncMock) as mock_curl,
            patch.object(scraper, "_fetch_with_playwright", new_callable=AsyncMock) as mock_pw,
        ):
            mock_api.return_value = None
            mock_curl.return_value = None
            mock_pw.return_value = None
            result = await scraper._execute_strategy_funnel(query="q", page_num=1)
        assert result == []

    async def test_curl_returns_invalid_html_falls_back_to_playwright(self, scraper):
        with (
            patch.object(scraper, "_fetch_with_api", new_callable=AsyncMock) as mock_api,
            patch.object(scraper, "_fetch_with_curl", new_callable=AsyncMock) as mock_curl,
            patch.object(scraper, "_fetch_with_playwright", new_callable=AsyncMock) as mock_pw,
        ):
            mock_api.return_value = None
            mock_curl.return_value = "<html><body>hi</body></html>"
            mock_pw.return_value = _wrap(PRIMARY_ITEM)
            result = await scraper._execute_strategy_funnel(query="q", page_num=1)
        assert len(result) == 1

    async def test_playwright_returns_invalid_html_returns_empty(self, scraper):
        with (
            patch.object(scraper, "_fetch_with_api", new_callable=AsyncMock) as mock_api,
            patch.object(scraper, "_fetch_with_curl", new_callable=AsyncMock) as mock_curl,
            patch.object(scraper, "_fetch_with_playwright", new_callable=AsyncMock) as mock_pw,
        ):
            mock_api.return_value = None
            mock_curl.return_value = None
            mock_pw.return_value = "<html><body>hi</body></html>"
            result = await scraper._execute_strategy_funnel(query="q", page_num=1)
        assert result == []


class TestSearchProductsPagination:
    async def test_stops_when_a_page_returns_no_products(self, scraper, monkeypatch):
        monkeypatch.setattr("mercadolibre_scraper.asyncio.sleep", AsyncMock())
        with patch.object(scraper, "_execute_strategy_funnel", new_callable=AsyncMock) as mock_funnel:
            mock_funnel.side_effect = [[{"id": "a", "title": "A"}], []]
            products = await scraper.search_products(query="q", max_pages=5)
        assert len(products) == 1
        assert mock_funnel.await_count == 2

    async def test_stops_on_critical_failure_none(self, scraper, monkeypatch):
        monkeypatch.setattr("mercadolibre_scraper.asyncio.sleep", AsyncMock())
        with patch.object(scraper, "_execute_strategy_funnel", new_callable=AsyncMock) as mock_funnel:
            mock_funnel.side_effect = [[{"id": "a", "title": "A"}], None]
            products = await scraper.search_products(query="q", max_pages=5)
        assert len(products) == 1

    async def test_stops_on_first_page_with_zero_products(self, scraper, monkeypatch):
        monkeypatch.setattr("mercadolibre_scraper.asyncio.sleep", AsyncMock())
        with patch.object(scraper, "_execute_strategy_funnel", new_callable=AsyncMock) as mock_funnel:
            mock_funnel.return_value = []
            products = await scraper.search_products(query="q", max_pages=5)
        assert products == []
        mock_funnel.assert_awaited_once()

    async def test_sleeps_between_pages_but_not_after_last(self, scraper, monkeypatch):
        sleep_mock = AsyncMock()
        monkeypatch.setattr("mercadolibre_scraper.asyncio.sleep", sleep_mock)
        with patch.object(scraper, "_execute_strategy_funnel", new_callable=AsyncMock) as mock_funnel:
            mock_funnel.side_effect = [
                [{"id": "a", "title": "A"}],
                [{"id": "b", "title": "B"}],
            ]
            await scraper.search_products(query="q", max_pages=2)
        # Sleeps once between page 1 and page 2, not after the final page.
        sleep_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# Light smoke coverage of the network-wrapper cache short-circuit.
# ---------------------------------------------------------------------------


class TestFetchWithCurlSmoke:
    async def test_cache_hit_skips_network_call(self, scraper):
        url = scraper._get_url("q", 1)
        scraper.cache.set(url, "<html>cached</html>")
        result = await scraper._fetch_with_curl("q", 1)
        assert result == "<html>cached</html>"
