"""tests/test_debug_utils.py — Unit tests for the shared debug_utils infra.

debug_utils.py is marked "do not modify" in the README/CLAUDE.md, so these
tests exercise it strictly as a black box — no source changes, no pragmas.
Every function here is pure, disk-local, or wraps a caller-supplied callable
(retry_with_backoff), so full coverage is achievable without any live
network access or a Playwright browser.

Run with:
    uv run pytest tests/test_debug_utils.py -v
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

import debug_utils


# ---------------------------------------------------------------------------
# User-agent / header helpers
# ---------------------------------------------------------------------------


class TestUserAgentHelpers:
    def test_get_random_user_agent_desktop(self):
        ua = debug_utils.get_random_user_agent(is_mobile=False)
        assert ua in debug_utils.DESKTOP_USER_AGENTS

    def test_get_random_user_agent_mobile(self):
        ua = debug_utils.get_random_user_agent(is_mobile=True)
        assert ua in debug_utils.MOBILE_USER_AGENTS

    def test_headers_non_chrome_ua_has_no_sec_ch_ua(self):
        firefox_ua = "Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0"
        with patch("debug_utils.get_random_user_agent", return_value=firefox_ua):
            headers = debug_utils.get_realistic_headers(is_mobile=False)
        assert headers["User-Agent"] == firefox_ua
        assert "sec-ch-ua" not in headers

    def test_headers_chrome_130_desktop(self):
        chrome_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0.0.0 Safari/537.36"
        with patch("debug_utils.get_random_user_agent", return_value=chrome_ua):
            headers = debug_utils.get_realistic_headers(is_mobile=False)
        assert '"Chromium";v="130"' in headers["sec-ch-ua"]
        assert headers["sec-ch-ua-mobile"] == "?0"
        assert headers["sec-ch-ua-platform"] == '"Windows"'

    def test_headers_chrome_129_mobile_android(self):
        chrome_ua = "Mozilla/5.0 (Linux; Android 14; SM-G998B) Chrome/129.0.0.0 Mobile Safari/537.36"
        with patch("debug_utils.get_random_user_agent", return_value=chrome_ua):
            headers = debug_utils.get_realistic_headers(is_mobile=True)
        assert '"Chromium";v="129"' in headers["sec-ch-ua"]
        assert headers["sec-ch-ua-mobile"] == "?1"
        assert headers["sec-ch-ua-platform"] == '"Android"'

    def test_headers_chrome_mobile_non_android_falls_back_to_windows(self):
        chrome_ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) Chrome/130.0.0.0 Mobile Safari/537.36"
        with patch("debug_utils.get_random_user_agent", return_value=chrome_ua):
            headers = debug_utils.get_realistic_headers(is_mobile=True)
        assert headers["sec-ch-ua-platform"] == '"Windows"'


# ---------------------------------------------------------------------------
# retry_with_backoff
# ---------------------------------------------------------------------------


class TestRetryWithBackoff:
    async def test_succeeds_on_first_attempt(self):
        func = AsyncMock(return_value="ok")
        result = await debug_utils.retry_with_backoff(func, max_retries=3)
        assert result == "ok"
        func.assert_awaited_once()

    async def test_succeeds_after_failures(self, monkeypatch):
        monkeypatch.setattr(debug_utils.asyncio, "sleep", AsyncMock())
        func = AsyncMock(side_effect=[ValueError("boom"), ValueError("boom"), "ok"])
        result = await debug_utils.retry_with_backoff(
            func, max_retries=3, base_delay=0.01, jitter=True
        )
        assert result == "ok"
        assert func.await_count == 3

    async def test_exhausts_retries_returns_none(self, monkeypatch):
        monkeypatch.setattr(debug_utils.asyncio, "sleep", AsyncMock())
        func = AsyncMock(side_effect=ValueError("always fails"))
        result = await debug_utils.retry_with_backoff(
            func, max_retries=2, base_delay=0.01
        )
        assert result is None
        assert func.await_count == 3  # initial attempt + 2 retries

    async def test_no_jitter_branch(self, monkeypatch):
        monkeypatch.setattr(debug_utils.asyncio, "sleep", AsyncMock())
        func = AsyncMock(side_effect=[ValueError("boom"), "ok"])
        result = await debug_utils.retry_with_backoff(
            func, max_retries=1, base_delay=0.01, jitter=False
        )
        assert result == "ok"


# ---------------------------------------------------------------------------
# validate_product_data
# ---------------------------------------------------------------------------


class TestValidateProductData:
    def test_missing_id_returns_none(self):
        assert debug_utils.validate_product_data({"title": "X"}) is None

    def test_blank_id_returns_none(self):
        assert debug_utils.validate_product_data({"id": "   ", "title": "X"}) is None

    def test_missing_title_returns_none(self):
        assert debug_utils.validate_product_data({"id": "1"}) is None

    def test_non_string_title_returns_none(self):
        assert debug_utils.validate_product_data({"id": "1", "title": 12345}) is None

    def test_title_truncated_to_200_chars(self):
        product = {"id": "1", "title": "x" * 300}
        cleaned = debug_utils.validate_product_data(product)
        assert len(cleaned["title"]) == 200

    def test_price_none_stays_none(self):
        cleaned = debug_utils.validate_product_data({"id": "1", "title": "T", "price": None})
        assert cleaned["price"] is None

    def test_price_negative_becomes_none(self):
        cleaned = debug_utils.validate_product_data({"id": "1", "title": "T", "price": -5.0})
        assert cleaned["price"] is None

    def test_price_unparseable_becomes_none(self):
        cleaned = debug_utils.validate_product_data(
            {"id": "1", "title": "T", "price": ["not", "a", "number"]}
        )
        assert cleaned["price"] is None

    def test_price_valid(self):
        cleaned = debug_utils.validate_product_data({"id": "1", "title": "T", "price": 42.5})
        assert cleaned["price"] == 42.5

    def test_url_missing(self):
        cleaned = debug_utils.validate_product_data({"id": "1", "title": "T"})
        assert cleaned["url"] is None

    def test_url_non_string(self):
        cleaned = debug_utils.validate_product_data({"id": "1", "title": "T", "url": 12345})
        assert cleaned["url"] is None

    def test_url_wrong_scheme_becomes_none(self):
        cleaned = debug_utils.validate_product_data(
            {"id": "1", "title": "T", "url": "ftp://example.com"}
        )
        assert cleaned["url"] is None

    def test_url_valid_http(self):
        cleaned = debug_utils.validate_product_data(
            {"id": "1", "title": "T", "url": "https://example.com/p"}
        )
        assert cleaned["url"] == "https://example.com/p"

    def test_rating_none(self):
        cleaned = debug_utils.validate_product_data({"id": "1", "title": "T", "rating": None})
        assert cleaned["rating"] is None

    def test_rating_in_range(self):
        cleaned = debug_utils.validate_product_data({"id": "1", "title": "T", "rating": 4.2})
        assert cleaned["rating"] == 4.2

    def test_rating_out_of_range_becomes_none(self):
        cleaned = debug_utils.validate_product_data({"id": "1", "title": "T", "rating": 9.9})
        assert cleaned["rating"] is None

    def test_rating_unparseable_becomes_none(self):
        cleaned = debug_utils.validate_product_data({"id": "1", "title": "T", "rating": "n/a"})
        assert cleaned["rating"] is None

    def test_review_count_none(self):
        cleaned = debug_utils.validate_product_data(
            {"id": "1", "title": "T", "review_count": None}
        )
        assert cleaned["review_count"] is None

    def test_review_count_valid(self):
        cleaned = debug_utils.validate_product_data(
            {"id": "1", "title": "T", "review_count": 10}
        )
        assert cleaned["review_count"] == 10

    def test_review_count_negative_becomes_none(self):
        cleaned = debug_utils.validate_product_data(
            {"id": "1", "title": "T", "review_count": -3}
        )
        assert cleaned["review_count"] is None

    def test_review_count_unparseable_becomes_none(self):
        cleaned = debug_utils.validate_product_data(
            {"id": "1", "title": "T", "review_count": "lots"}
        )
        assert cleaned["review_count"] is None

    def test_source_and_currency_passthrough(self):
        cleaned = debug_utils.validate_product_data(
            {"id": "1", "title": "T", "source": "Amazon", "currency": "USD"}
        )
        assert cleaned["source"] == "Amazon"
        assert cleaned["currency"] == "USD"


# ---------------------------------------------------------------------------
# is_retryable_error
# ---------------------------------------------------------------------------


class TestIsRetryableError:
    def test_retryable_status_code(self):
        assert debug_utils.is_retryable_error(503) is True

    def test_non_retryable_status_code_no_exception(self):
        assert debug_utils.is_retryable_error(404) is False

    def test_retryable_exception_type(self):
        assert debug_utils.is_retryable_error(None, TimeoutError("slow")) is True

    def test_non_retryable_exception_type(self):
        assert debug_utils.is_retryable_error(None, ValueError("bad")) is False

    def test_no_status_no_exception(self):
        assert debug_utils.is_retryable_error(None, None) is False


# ---------------------------------------------------------------------------
# FileCache
# ---------------------------------------------------------------------------


class TestFileCache:
    def test_miss_when_absent(self, tmp_path):
        cache = debug_utils.FileCache(cache_dir=str(tmp_path / "c"), max_age_hours=1)
        assert cache.get("http://example.com/x") is None

    def test_set_then_hit(self, tmp_path):
        cache = debug_utils.FileCache(cache_dir=str(tmp_path / "c"), max_age_hours=1)
        cache.set("http://example.com/x", "<html>hi</html>")
        assert cache.get("http://example.com/x") == "<html>hi</html>"

    def test_expired_entry_is_a_miss(self, tmp_path):
        cache = debug_utils.FileCache(cache_dir=str(tmp_path / "c"), max_age_hours=1)
        url = "http://example.com/x"
        path = cache._get_cache_path(url)
        stale = {
            "timestamp": (datetime.utcnow() - timedelta(hours=5)).isoformat(),
            "url": url,
            "html": "<html>old</html>",
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(stale, f)
        assert cache.get(url) is None

    def test_corrupt_json_is_a_miss(self, tmp_path):
        cache = debug_utils.FileCache(cache_dir=str(tmp_path / "c"), max_age_hours=1)
        url = "http://example.com/x"
        path = cache._get_cache_path(url)
        with open(path, "w", encoding="utf-8") as f:
            f.write("not valid json{{{")
        assert cache.get(url) is None

    def test_missing_keys_is_a_miss(self, tmp_path):
        cache = debug_utils.FileCache(cache_dir=str(tmp_path / "c"), max_age_hours=1)
        url = "http://example.com/x"
        path = cache._get_cache_path(url)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"url": url}, f)  # missing "timestamp"/"html"
        assert cache.get(url) is None


# ---------------------------------------------------------------------------
# Debug / logging helpers
# ---------------------------------------------------------------------------


class TestDebugAndLoggingHelpers:
    def test_setup_logging_does_not_raise(self):
        debug_utils.setup_logging()

    def test_save_debug_html_writes_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        debug_utils.save_debug_html("<html>debug</html>", "unit_test")
        saved = list((tmp_path / "debug_pages").glob("debug_unit_test_*.html"))
        assert len(saved) == 1
        assert saved[0].read_text(encoding="utf-8") == "<html>debug</html>"

    def test_save_debug_html_handles_write_failure(self, tmp_path, monkeypatch, caplog):
        monkeypatch.chdir(tmp_path)

        def raising_open(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(debug_utils, "open", raising_open, raising=False)
        with caplog.at_level(logging.ERROR):
            debug_utils.save_debug_html("<html>x</html>", "unit_test")
        assert any("Could not save HTML file" in r.message for r in caplog.records)

    def test_log_html_snippet(self, caplog):
        logger = logging.getLogger("test.logger")
        with caplog.at_level(logging.WARNING):
            debug_utils.log_html_snippet(logger, "Amazon", "price", "<div>a" * 200)
        assert any("PARSE FAIL" in r.message for r in caplog.records)

    def test_print_header_does_not_raise(self):
        debug_utils.print_header()


class TestDependencyAndErrorHandling:
    def test_check_dependencies_all_present(self):
        with patch("debug_utils.metadata.version", return_value="1.0.0"):
            debug_utils.check_dependencies()  # must not raise

    def test_check_dependencies_missing_package_exits(self):
        def fake_version(name):
            if name == "pandas":
                raise debug_utils.metadata.PackageNotFoundError(name)
            return "1.0.0"

        with patch("debug_utils.metadata.version", side_effect=fake_version):
            with pytest.raises(SystemExit):
                debug_utils.check_dependencies()

    def test_handle_critical_error_exits(self, caplog):
        with caplog.at_level(logging.CRITICAL):
            with pytest.raises(SystemExit):
                debug_utils.handle_critical_error(RuntimeError("kaboom"))
