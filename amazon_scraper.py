import asyncio
import logging
import random
import re
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urljoin

import httpx
from selectolax.parser import HTMLParser, Node
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

try:
    from playwright_stealth import stealth_async
except ImportError:
    stealth_async = None

from debug_utils import (
    FileCache,
    get_random_user_agent,
    get_realistic_headers,
    retry_with_backoff,
    validate_product_data,
    is_retryable_error,
    log_html_snippet,
    save_debug_html,
    console,
)

logger = logging.getLogger(__name__)


class AmazonScraper:
    def __init__(self) -> None:
        self.base_url = "https://www.amazon.com"
        self.cache = FileCache()

    async def search_products(self, query: str, max_pages: int) -> List[Dict[str, Any]]:
        logger.info(f"--- Starting Amazon Search for '{query}' ---")
        async with httpx.AsyncClient(
            http2=True, timeout=30.0, follow_redirects=True, verify=False
        ) as client:
            all_products = []
            for page_num in range(1, max_pages + 1):
                console.log(f"[Amazon] Scraping page {page_num}/{max_pages} for '{query}'...")
                products = await self._execute_strategy_funnel(client, query, page_num)
                if products is None:
                    logger.warning(
                        f"[Amazon] Page {page_num}: Critical failure. Stopping search."
                    )
                    break
                all_products.extend(products)
                if not products:
                    if page_num == 1:
                        logger.warning("[Amazon] Page 1: No products found after all strategies. Stopping search.")
                    else:
                        logger.info(f"[Amazon] Page {page_num}: No more products. Search complete.")
                    break

        final_products = self._deduplicate(all_products)
        logger.info(f"--- Amazon Search Finished. Found {len(final_products)} total products. ---")
        return final_products

    async def _execute_strategy_funnel(
        self, client: httpx.AsyncClient, query: str, page_num: int
    ) -> Optional[List[Dict[str, Any]]]:
        logger.info(f"[Amazon] Page {page_num}: Executing Strategy Funnel...")

        logger.info(f"[Amazon] Page {page_num}: -> [ATTEMPT] Strategy 3: Desktop HTTPX Request...")
        desktop_html = await self._fetch_with_httpx(client, query, page_num, is_mobile=False)
        if desktop_html:
            products, is_valid = self._parse_html(desktop_html, is_mobile=False)
            if is_valid and products:
                logger.info(f"[Amazon] Page {page_num}:    -> [SUCCESS] Strategy 3 SUCCEEDED. Found {len(products)} products.")
                return products
            logger.warning(f"[Amazon] Page {page_num}:    -> [RESULT] Strategy 3 FAILED. Page was invalid or no products found.")

        logger.warning(f"[Amazon] Page {page_num}: -> [ATTEMPT] Strategy 4: Mobile HTTPX Request...")
        mobile_html = await self._fetch_with_httpx(client, query, page_num, is_mobile=True)
        if mobile_html:
            products, is_valid = self._parse_html(mobile_html, is_mobile=True)
            if is_valid and products:
                logger.info(f"[Amazon] Page {page_num}:    -> [SUCCESS] Strategy 4 SUCCEEDED. Found {len(products)} products.")
                return products
            logger.warning(f"[Amazon] Page {page_num}:    -> [RESULT] Strategy 4 FAILED. Page was invalid or no products found.")

        logger.warning(f"[Amazon] Page {page_num}: -> [ATTEMPT] Strategy 5: Playwright Full Browser...")
        playwright_html = await self._fetch_with_playwright(query, page_num)
        if playwright_html:
            products, is_valid = self._parse_html(playwright_html, is_mobile=False)
            if is_valid and products:
                logger.info(f"[Amazon] Page {page_num}:    -> [SUCCESS] Strategy 5 SUCCEEDED. Found {len(products)} products.")
                return products
            logger.warning(f"[Amazon] Page {page_num}:    -> [RESULT] Strategy 5 FAILED. Page was invalid or no products found.")

        logger.error(f"[Amazon] Page {page_num}: All strategies FAILED.")
        return []

    async def _fetch_with_httpx(
        self, client: httpx.AsyncClient, query: str, page_num: int, is_mobile: bool
    ) -> Optional[str]:
        url = self._get_url(query, page_num, is_mobile)
        if (html := self.cache.get(url)) is not None:
            return html

        headers = get_realistic_headers(is_mobile)

        async def make_request() -> str:
            response = await client.get(url, headers=headers)
            logger.info(f"HTTP Request: GET {url} \"{response.status_code} {response.reason_phrase}\"")

            if response.status_code == 200:
                self.cache.set(url, response.text)
                return response.text
            elif is_retryable_error(response.status_code):
                raise httpx.HTTPStatusError(f"Retryable HTTP error: {response.status_code}", request=response.request, response=response)
            else:
                logger.warning(f"[Amazon] HTTPX fetch for {url} returned non-retryable status {response.status_code}")
                return None

        try:
            result = await retry_with_backoff(make_request, max_retries=3)
            return result
        except Exception as e:
            logger.error(f"[Amazon] HTTPX fetch failed for {url} after retries. Error: {e}")
            return None

    async def _fetch_with_playwright(self, query: str, page_num: int) -> Optional[str]:
        url = self._get_url(query, page_num, is_mobile=False)
        if (html := self.cache.get(url)) is not None:
            return html

        logger.info(f"[Amazon] Playwright navigating to: {url}")

        async def playwright_request() -> str:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        '--no-sandbox',
                        '--disable-setuid-sandbox',
                        '--disable-dev-shm-usage',
                        '--disable-accelerated-2d-canvas',
                        '--no-first-run',
                        '--no-zygote',
                        '--disable-gpu',
                        '--disable-blink-features=AutomationControlled'
                    ]
                )

                context = await browser.new_context(
                    user_agent=get_random_user_agent(is_mobile=False),
                    viewport={'width': 1366, 'height': 768},
                    extra_http_headers={
                        'Accept-Language': 'en-US,en;q=0.9',
                        'Accept-Encoding': 'gzip, deflate, br',
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                    }
                )

                pw_page = await context.new_page()

                if stealth_async:
                    await stealth_async(pw_page)
                else:
                    await pw_page.add_init_script("""
                        Object.defineProperty(navigator, 'webdriver', {
                            get: () => undefined,
                        });
                        window.chrome = { runtime: {} };
                    """)

                await pw_page.goto(url, timeout=45000, wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(2, 4))

                if await pw_page.locator('form[action="/errors/validateCaptcha"]').count() > 0:
                    logger.warning("[Amazon] Playwright Page Analysis: CAPTCHA detected.")
                    save_debug_html(await pw_page.content(), "amazon_playwright_captcha")
                    await browser.close()
                    raise Exception("CAPTCHA detected")

                if "robot" in (await pw_page.content()).lower():
                    logger.warning("[Amazon] Playwright Page Analysis: Robot detection page.")
                    save_debug_html(await pw_page.content(), "amazon_robot_detected")
                    await browser.close()
                    raise Exception("Robot detection triggered")

                selectors = [
                    'div[data-component-type="s-search-results"]',
                    '[data-testid="search-results"]',
                    '.s-main-slot',
                    '#search'
                ]

                result_found = False
                for selector in selectors:
                    try:
                        await pw_page.wait_for_selector(selector, timeout=10000)
                        result_found = True
                        break
                    except PlaywrightTimeoutError:
                        continue

                if not result_found:
                    save_debug_html(await pw_page.content(), "amazon_no_results")
                    await browser.close()
                    raise Exception("No search results container found")

                html = await pw_page.content()
                await browser.close()
                self.cache.set(url, html)
                return html

        try:
            result = await retry_with_backoff(playwright_request, max_retries=2)
            return result
        except Exception as e:
            logger.error(f"[Amazon] Playwright fetch failed for {url} after retries. Error: {e}")
            return None

    def _parse_html(self, html: str, is_mobile: bool) -> Tuple[List[Dict[str, Any]], bool]:
        tree = HTMLParser(html)
        if not self._is_valid_page(tree):
            save_debug_html(html, "amazon_invalid_page")
            return [], False

        selectors = [
            'div[data-component-type="s-search-result"]',
            'div.s-result-item[data-asin]',
        ]
        container = next((tree.css(s) for s in selectors if tree.css(s)), [])

        products = []
        for item in container:
            product = self._extract_product_info(item)
            if product:
                products.append(product)
        return products, True

    def _is_valid_page(self, tree: HTMLParser) -> bool:
        logger.info("[Amazon] Page Analysis: Validating page content...")
        if tree.css_first('form[action="/errors/validateCaptcha"]'):
            logger.warning("[Amazon] Page Analysis: CAPTCHA detected.")
            return False
        if "No results for" in (tree.css_first("h1, .a-row") or Node()).text():
            logger.warning("[Amazon] Page Analysis: 'No results' page detected.")
            return False
        logger.info("[Amazon] Page Analysis: Page appears valid.")
        return True

    def _extract_product_info(self, item: Node) -> Optional[Dict[str, Any]]:
        asin = item.attributes.get("data-asin")
        if not asin:
            return None

        def get_text(selector: str, default: Any = None) -> Optional[str]:
            node = item.css_first(selector)
            return node.text(strip=True) if node else default

        title_selectors = [
            'h2 a span',
            'h2 span',
            'h2 a',
            '.a-link-normal span',
            '[data-cy="title-recipe-title"]'
        ]
        title = None
        for selector in title_selectors:
            title = get_text(selector)
            if title:
                break

        if not title:
            log_html_snippet(logger, "Amazon", "title", item.html)
            return None

        price_selectors = [
            ('span.a-price-whole', 'span.a-price-fraction'),
            ('span.a-price .a-offscreen', None),
            ('.a-price-range .a-offscreen', None)
        ]

        price = None
        for whole_sel, frac_sel in price_selectors:
            if frac_sel:
                price_whole = get_text(whole_sel)
                price_fraction = get_text(frac_sel)
                if price_whole and price_fraction:
                    try:
                        price = float(f"{price_whole.replace(',', '')}.{price_fraction}")
                        break
                    except ValueError:
                        continue
            else:
                price_text = get_text(whole_sel)
                if price_text:
                    try:
                        price_match = re.search(r'[\d,]+\.?\d*', price_text.replace(',', ''))
                        if price_match:
                            price = float(price_match.group())
                            break
                    except ValueError:
                        continue

        if price is None:
            log_html_snippet(logger, "Amazon", "price", item.html)

        rating_selectors = [
            "span.a-icon-alt",
            "[aria-label*='out of']",
            "[data-cy*='rating']"
        ]
        rating = None
        for selector in rating_selectors:
            rating_text = get_text(selector)
            if rating_text:
                try:
                    rating_match = re.search(r'(\d+\.?\d*)\s*out of', rating_text)
                    if rating_match:
                        rating = float(rating_match.group(1))
                        break
                    rating_match = re.search(r'(\d+\.?\d*)', rating_text)
                    if rating_match:
                        rating = float(rating_match.group(1))
                        break
                except (ValueError, IndexError):
                    continue

        review_selectors = [
            'span.a-size-base[dir="auto"]',
            'a[href*="#reviews"] span',
            '[data-cy*="reviews"]'
        ]
        review_count = None
        for selector in review_selectors:
            review_text = get_text(selector)
            if review_text:
                try:
                    review_match = re.search(r'([\d,]+)', review_text.replace('(', '').replace(')', ''))
                    if review_match:
                        review_count = int(review_match.group(1).replace(',', ''))
                        break
                except ValueError:
                    continue

        url_selectors = [
            "a.a-link-normal.s-no-outline",
            "a.a-link-normal",
            "h2 a",
            "a[href*='/dp/']"
        ]
        url = None
        for selector in url_selectors:
            url_node = item.css_first(selector)
            if url_node and url_node.attributes.get("href"):
                url = urljoin(self.base_url, url_node.attributes["href"])
                break

        if not url:
            url = f"{self.base_url}/dp/{asin}"

        is_ad = bool(
            item.css_first('.puis-sponsored-label-info-icon') or
            item.css_first('.s-sponsored-label-info-icon') or
            item.css_first('[aria-label="Sponsored"]') or
            item.css_first('[aria-label="Patrocinado"]')
        )

        raw_product = {
            "id": asin,
            "source": "Amazon",
            "title": title,
            "url": url,
            "price": price,
            "currency": get_text("span.a-price-symbol", default="$"),
            "rating": rating,
            "review_count": review_count,
            "is_ad": is_ad,
        }

        validated_product = validate_product_data(raw_product)
        return validated_product

    def _get_url(self, query: str, page_num: int, is_mobile: bool) -> str:
        q = query.replace(" ", "+")
        if is_mobile:
            return f"https://www.amazon.com/gp/aw/s?k={q}&page={page_num}"
        return f"https://www.amazon.com/s?k={q}&page={page_num}"

    def _get_headers(self, is_mobile: bool) -> Dict[str, str]:
        return get_realistic_headers(is_mobile)

    def _deduplicate(self, products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return list({p["id"]: p for p in products}.values())