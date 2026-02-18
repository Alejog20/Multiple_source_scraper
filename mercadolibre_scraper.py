import asyncio
import logging
import random
import re
from typing import List, Dict, Any, Optional, Tuple
import json # Added: Required for JSON parsing

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


class MercadoLibreScraper:
    def __init__(self, country_code: str = "co") -> None:
        self.base_url = f"https://www.mercadolibre.com.{country_code}"
        self.api_url = f"https://api.mercadolibre.com/sites/MCO/search"
        self.cache = FileCache()

    async def search_products(self, query: str, max_pages: int) -> List[Dict[str, Any]]:
        logger.info(f"--- Starting MercadoLibre Search for '{query}' ---")
        async with httpx.AsyncClient(
            http2=True, timeout=30.0, follow_redirects=True, verify=False
        ) as client:
            all_products = []
            for page_num in range(1, max_pages + 1):
                console.log(f"[MercadoLibre] Scraping page {page_num}/{max_pages} for '{query}'...")
                products = await self._execute_strategy_funnel(client, query, page_num)
                if products is None:
                    logger.warning(f"[MercadoLibre] Page {page_num}: Critical failure. Stopping search.")
                    break
                all_products.extend(products)
                if not products and page_num == 1:
                    logger.warning("[MercadoLibre] Page 1: No products found. Stopping search.")
                    break

        final_products = self._deduplicate(all_products)
        logger.info(f"--- MercadoLibre Search Finished. Found {len(final_products)} total products. ---")
        return final_products

    async def _execute_strategy_funnel(
        self, client: httpx.AsyncClient, query: str, page_num: int
    ) -> Optional[List[Dict[str, Any]]]:
        logger.info(f"[MercadoLibre] Page {page_num}: Executing Strategy Funnel...")

        logger.info(f"[MercadoLibre] Page {page_num}: -> [ATTEMPT] Strategy 2: API Request...")
        api_data = await self._fetch_with_api(client, query, page_num)
        if api_data:
            products = self._parse_api_data(api_data)
            if products:
                logger.info(f"[MercadoLibre] Page {page_num}:    -> [SUCCESS] Strategy 2 SUCCEEDED. Found {len(products)} products.")
                return products
            logger.warning(f"[MercadoLibre] Page {page_num}:    -> [RESULT] API returned no products.")

        logger.warning(f"[MercadoLibre] Page {page_num}: -> [ATTEMPT] Strategy 3: Desktop HTTPX...")
        desktop_html = await self._fetch_with_html(client, query, page_num)
        if desktop_html:
            products, is_valid = self._parse_html(desktop_html)
            if is_valid and products:
                logger.info(f"[MercadoLibre] Page {page_num}:    -> [SUCCESS] Strategy 3 SUCCEEDED. Found {len(products)} products.")
                return products
            logger.warning(f"[MercadoLibre] Page {page_num}:    -> [RESULT] Strategy 3 FAILED. Page invalid or no products.")

        logger.warning(f"[MercadoLibre] Page {page_num}: -> [ATTEMPT] Strategy 4: Playwright Full Browser...")
        playwright_html = await self._fetch_with_playwright(query, page_num)
        if playwright_html:
            products, is_valid = self._parse_html(playwright_html)
            if is_valid and products:
                logger.info(f"[MercadoLibre] Page {page_num}:    -> [SUCCESS] Strategy 4 SUCCEEDED. Found {len(products)} products.")
                return products
            logger.warning(f"[MercadoLibre] Page {page_num}:    -> [RESULT] Strategy 4 FAILED. Page invalid or no products.")

        logger.error(f"[MercadoLibre] Page {page_num}: All strategies FAILED.")
        return []

    async def _fetch_with_api(
        self, client: httpx.AsyncClient, query: str, page_num: int
    ) -> Optional[Dict[str, Any]]:
        offset = (page_num - 1) * 50
        params = {"q": query, "offset": offset, "limit": 50}
        try:
            response = await client.get(self.api_url, params=params)
            logger.info(f"HTTP Request: GET {response.url} \"{response.status_code} {response.reason_phrase}\"")
            if response.status_code == 200:
                return response.json()
            logger.warning(f"[MercadoLibre] API fetch returned status {response.status_code}")
            return None
        except httpx.RequestError as e:
            logger.error(f"[MercadoLibre] API fetch failed. Error: {e}")
            return None

    async def _fetch_with_html(
        self, client: httpx.AsyncClient, query: str, page_num: int
    ) -> Optional[str]:
        url = self._get_url(query, page_num)
        if (html := self.cache.get(url)) is not None:
            return html

        headers = get_realistic_headers(is_mobile=False)

        async def make_request() -> str:
            response = await client.get(url, headers=headers)
            logger.info(f"HTTP Request: GET {url} \"{response.status_code} {response.reason_phrase}\"")

            if response.status_code == 200:
                self.cache.set(url, response.text)
                return response.text
            elif is_retryable_error(response.status_code):
                raise httpx.HTTPStatusError(f"Retryable HTTP error: {response.status_code}", request=response.request, response=response)
            else:
                logger.warning(f"[MercadoLibre] HTML fetch for {url} returned non-retryable status {response.status_code}")
                return None

        try:
            result = await retry_with_backoff(make_request, max_retries=3)
            return result
        except Exception as e:
            logger.error(f"[MercadoLibre] HTML fetch failed for {url} after retries. Error: {e}")
            return None

    async def _fetch_with_playwright(self, query: str, page_num: int) -> Optional[str]:
        url = self._get_url(query, page_num)
        logger.info(f"[MercadoLibre] Playwright navigating to: {url}")

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
                        'Accept-Language': 'es-CO,es;q=0.9,en-US;q=0.8,en;q=0.7',
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

                await pw_page.goto(url, timeout=60000, wait_until="networkidle")
                await asyncio.sleep(random.uniform(2, 4))

                # Check for JavaScript challenge page (spinner/challenge)
                content = await pw_page.content()
                if "verifyChallenge" in content or '<div class="spinner">' in content:
                    logger.info("[MercadoLibre] Playwright: Challenge page detected, waiting for resolution...")
                    # Wait longer for challenge to complete and page to reload
                    await asyncio.sleep(5)
                    try:
                        await pw_page.wait_for_load_state("networkidle", timeout=30000)
                    except PlaywrightTimeoutError:
                        pass

                # Wait for product containers to appear
                selectors = [
                    'li.ui-search-layout__item',
                    '.poly-card',
                    '.ui-search-results',
                    '[data-testid="search-result"]'
                ]

                result_found = False
                for selector in selectors:
                    try:
                        await pw_page.wait_for_selector(selector, timeout=15000)
                        result_found = True
                        logger.info(f"[MercadoLibre] Playwright: Found results container with selector: {selector}")
                        break
                    except PlaywrightTimeoutError:
                        continue

                if not result_found:
                    save_debug_html(await pw_page.content(), "mercadolibre_playwright_no_results")
                    await browser.close()
                    raise Exception("No search results container found after challenge")

                html = await pw_page.content()
                save_debug_html(html, "mercadolibre_playwright_successful_fetch") # DEBUG: Save fetched HTML
                await browser.close()
                # Cache the successful HTML
                self.cache.set(url, html)
                return html

        try:
            result = await retry_with_backoff(playwright_request, max_retries=2)
            return result
        except Exception as e:
            logger.error(f"[MercadoLibre] Playwright fetch failed for query '{query}' page {page_num} after retries. Error: {e}")
            return None

    def _parse_api_data(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        results = data.get("results", [])
        if not results:
            return []

        products = []
        for item in results:
            products.append({
                "id": item.get("id"),
                "source": "MercadoLibre",
                "title": item.get("title"),
                "url": item.get("permalink"),
                "price": item.get("price"),
                "currency": item.get("currency_id"),
                "rating": None,
                "review_count": None,
            })
        return products

    def _parse_html(self, html: str) -> Tuple[List[Dict[str, Any]], bool]:
        tree = HTMLParser(html)
        tree = HTMLParser(html)
        script_tag = tree.css_first('script#___NEXT_DATA__') or tree.css_first('script#__NORDIC_RENDERING_CTX__')

        if script_tag and script_tag.text():
            try:
                # Extract the JSON string. For __NORDIC_RENDERING_CTX__, it's within _n.ctx.r = {...}
                # For ___NEXT_DATA__, it's directly JSON
                json_data_str = script_tag.text()
                if script_tag.tag == 'script' and script_tag.attributes.get('id') == '__NORDIC_RENDERING_CTX__':
                    # The script content is like 'window._n.ctx.r={...};' or '_n.ctx.r={...};'
                    assignment_prefix = '_n.ctx.r='
                    start_idx_prefix = json_data_str.find(assignment_prefix)
                    if start_idx_prefix != -1:
                        json_data_str = json_data_str[start_idx_prefix + len(assignment_prefix):].strip()
                        # After extracting the assigned value, ensure no trailing semicolon
                        if json_data_str.endswith(';'):
                            json_data_str = json_data_str[:-1]
                    else:
                        logger.error("[MercadoLibre] __NORDIC_RENDERING_CTX__ script content does not start with expected assignment.")
                        return [], False
                
                # DEBUG PRINT
                # logger.info(f"DEBUG: json_data_str length: {len(json_data_str)}")
                # logger.info(f"DEBUG: json_data_str content (first 500 chars): {json_data_str[:500]}")
                # logger.info(f"DEBUG: json_data_str content (last 500 chars): {json_data_str[-500:]}")
                
                data = json.loads(json_data_str)
                
                # Navigate through the JSON structure to find the product results
                products_data_raw = []
                if 'appProps' in data and 'pageProps' in data['appProps'] and \
                   'initialState' in data['appProps']['pageProps'] and \
                   'results' in data['appProps']['pageProps']['initialState']:
                    products_data_raw = data['appProps']['pageProps']['initialState']['results']
                # Add other potential paths if necessary, e.g., for ___NEXT_DATA__
                elif 'props' in data and 'pageProps' in data['props'] and \
                     'initialState' in data['props']['pageProps'] and \
                     'results' in data['props']['pageProps']['initialState']:
                    products_data_raw = data['props']['pageProps']['initialState']['results']
                elif 'query' in data and 'results' in data['query']:
                    products_data_raw = data['query']['results']


                if not products_data_raw:
                    logger.warning("[MercadoLibre] PARSE: Could not find product data within JSON structure.")
                    save_debug_html(html, "mercadolibre_no_json_products")
                    return [], False

                products = []
                for item_data in products_data_raw:
                    product_data_to_extract = item_data
                    if item_data.get('id') == 'POLYCARD' and 'polycard' in item_data:
                        product_data_to_extract = item_data['polycard']
                    
                    product = self._extract_product_from_json_data_item(product_data_to_extract)
                    if product:
                        products.append(product)

                logger.info(f"[MercadoLibre] PARSE: Successfully extracted {len(products)} products from JSON.")
                return products, True

            except json.JSONDecodeError as e:
                logger.error(f"[MercadoLibre] JSON Decode Error: {e}")
                save_debug_html(html, "mercadolibre_json_decode_error")
                return [], False
            except KeyError as e:
                logger.error(f"[MercadoLibre] JSON Key Error: Missing key in structure - {e}")
                save_debug_html(html, "mercadolibre_json_key_error")
                return [], False

        # Fallback to HTML parsing if no script tag or JSON data found (less likely to work)
        if not self._is_valid_page(tree):
            save_debug_html(html, "mercadolibre_invalid_page")
            return [], False

        selectors = [
            "li.ui-search-layout__item",
            "div.ui-search-result__wrapper",
            ".poly-card",
            ".ui-search-results__item",
            "div.ui-search-layout__item",
            "article.ui-search-result",
            "div[data-item-id]",
            "div[id*='searchResults'] .ui-search-result"
        ]
        container = []
        for s in selectors:
            found = tree.css(s)
            if found:
                container.extend(found)
                break # Take the first successful selector's results

        if not container:
            logger.warning("[MercadoLibre] PARSE: Could not find product containers with any selector.")
            save_debug_html(html, "mercadolibre_no_containers")
            return [], False

        logger.info(f"[MercadoLibre] PARSE: Found {len(container)} product containers.")
        products = []
        for item in container:
            product = self._extract_product_info_from_html(item)
            if product:
                products.append(product)

        logger.info(f"[MercadoLibre] PARSE: Successfully extracted {len(products)} products.")
        return products, True

    def _is_valid_page(self, tree: HTMLParser) -> bool:
        logger.info("[MercadoLibre] Page Analysis: Validating page content...")

        # Check for JavaScript challenge page (bot protection)
        html_text = tree.html or ""
        body_text = tree.body.text() if tree.body else ""
        
        # DEBUG PRINTS
        # print(f"DEBUG: body_text.strip() length: {len(body_text.strip())}")
        # print(f"DEBUG: body_text.strip() content: {body_text.strip()[:200]}...") # Print first 200 chars
        
        if "verifyChallenge" in html_text or '<div class="spinner">' in html_text:
            logger.warning("[MercadoLibre] Page Analysis: JavaScript challenge page detected.")
            return False

        # Check for "no results" message, specifically for Playwright to avoid false positives with challenge pages
        if "No hay publicaciones que coincidan con tu bÃºsqueda" in html_text and "ui-search-rescue__title" in html_text:
            logger.warning("[MercadoLibre] Page Analysis: 'No results' page detected (specific check).")
            return False

        # Check if page has minimal content (likely a challenge or error page)
        if len(body_text.strip()) < 100:
            logger.warning("[MercadoLibre] Page Analysis: Page has minimal content, likely invalid.")
            return False

        logger.info("[MercadoLibre] Page Analysis: Page appears valid.")
        return True

    def _extract_product_info_from_html(self, item: Node) -> Optional[Dict[str, Any]]:
        title = None
        title_selectors = [
            "h3.poly-component__title-wrapper > a.poly-component__title",
            "img[title]", # Added to capture titles from img tags
            "h2.ui-search-item__title > a", # Added for legacy title structure
            "div.ui-search-item__title > a", # More general title link
        ]

        for selector in title_selectors:
            if "img[title]" in selector: # Check for img[title] specific processing
                img_node = item.css_first(selector)
                if img_node and img_node.attributes.get("title"):
                    title = img_node.attributes["title"].strip()
                    break
            else:
                title_node = item.css_first(selector)
                if title_node and title_node.text(strip=True):
                    title = title_node.text(strip=True)
                    break

        if not title:
            log_html_snippet(logger, "MercadoLibre", "title", item.html)
            return None

        url = None
        url_selectors = [
            "h3.poly-component__title-wrapper > a.poly-component__title",
            "a.ui-search-link", # Added for poly-card structure
            "h2.ui-search-item__title > a", # Added for legacy title structure
            "li > div > a", # More general link within a list item for simpler cases
        ]

        for selector in url_selectors:
            url_node = item.css_first(selector)
            if url_node and url_node.attributes.get("href"):
                url = url_node.attributes["href"]
                break

        price = None
        price_selectors = [
            "div.poly-component__price div.poly-price__current span.andes-money-amount__fraction",
            "div.poly-price span.andes-money-amount__fraction", # Added for poly-card price structure
            "div.ui-search-price span.andes-money-amount__fraction", # Added for legacy price structure
            "span.price-tag-fraction", # Added for a different legacy price structure
            "div.price", # General selector for div with class price
        ]

        for selector in price_selectors:
            price_node = item.css_first(selector)
            if price_node and price_node.text(strip=True):
                try:
                    price_text = price_node.text(strip=True).replace('.', '') # Clean up for Colombian pesos
                    # Remove currency symbols before converting to float
                    price_text = price_text.replace('$', '').replace('COP', '').strip()
                    price = float(price_text)
                    break
                except (ValueError, TypeError):
                    continue

        if price is None:
            log_html_snippet(logger, "MercadoLibre", "price", item.html)

        product_id = None
        if url:
            try:
                # Prioritize 'id' from the URL path if available
                id_match_path = re.search(r'(MCO-\d+)', url)
                if id_match_path:
                    product_id = id_match_path.group(1)
                else:
                    # Fallback to older ID extraction logic for other patterns
                    id_match = re.search(r'(MLA|MCO)([0-9]+)', url)
                    if id_match:
                        product_id = id_match.group(0)
                    else:
                        # Attempt to extract ID from URL parts if no specific pattern found
                        url_parts = url.split('/')
                        # Look for a part that looks like an ID (e.g., "MCO123456789" or "123456789")
                        # This is a heuristic and might need refinement
                        for part in reversed(url_parts): # Check from the end of the URL
                            if re.match(r'^(MLA|MCO)?\d{9,}$', part): # At least 9 digits, optionally prefixed
                                product_id = part
                                break
                            elif re.match(r'^[A-Z]{3}\d+$', part): # e.g. MCO123
                                product_id = part
                                break
            except (IndexError, AttributeError):
                log_html_snippet(logger, "MercadoLibre", "product_id", item.html)
        
        # If product_id is still None, try to get from the parent item's attributes
        if not product_id:
            data_id = item.attributes.get("data-item-id")
            if data_id:
                product_id = data_id
            
            # As a last resort, if title is available, generate a hash or use a placeholder
            if not product_id and title:
                product_id = f"ML_GEN_{hash(title)}"


        currency = "$"
        currency_selectors = [
            "span.andes-money-amount__currency-symbol",
            ".andes-money-amount__currency-symbol",
            ".price-tag-symbol",
            ".poly-price__symbol",
            "div.price" # Added to capture currency symbol from text content if present
        ]

        for selector in currency_selectors:
            currency_node = item.css_first(selector)
            if currency_node and currency_node.text(strip=True):
                currency_text = currency_node.text(strip=True)
                if '$' in currency_text: # Simple check for common currency symbol
                    currency = '$'
                elif 'COP' in currency_text:
                    currency = 'COP'
                break

        raw_product = {
            "id": product_id,
            "source": "MercadoLibre",
            "title": title,
            "url": url,
            "price": price,
            "currency": currency,
            "rating": None,
            "review_count": None,
        }

        validated_product = validate_product_data(raw_product)
        return validated_product

    def _extract_product_from_json_data_item(self, item_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        product_id = None
        title = None
        url = None
        price = None
        currency = None

        # Expecting item_data to be the unwrapped 'polycard' content
        # It should contain 'metadata' and 'components' at its top level
        metadata = item_data.get('metadata', {})
        components = item_data.get('components', [])

        # Extract product_id and url from metadata
        product_id = metadata.get('id')
        url = metadata.get('url')

        # Extract title, price, and currency from components
        for component in components:
            component_type = component.get('type')
            if component_type == 'title':
                title_data = component.get('title', {})
                title = title_data.get('text')
            elif component_type == 'price':
                price_data = component.get('price', {})
                current_price_data = price_data.get('current_price', {})
                price_value = current_price_data.get('value')
                currency = current_price_data.get('currency')
                if price_value is not None:
                    try:
                        price = float(price_value)
                    except ValueError:
                        price = None
        
        # Check if the URL is relative and prepend base_url if necessary
        if url and url.startswith('www.'):
            url = f"https://{url}"
        elif url and url.startswith('/'):
            url = f"{self.base_url}{url}"

        raw_product = {
            "id": product_id,
            "source": "MercadoLibre",
            "title": title,
            "url": url,
            "price": price,
            "currency": currency,
            "rating": None,
            "review_count": None,
        }

        validated_product = validate_product_data(raw_product)
        return validated_product

    def _get_url(self, query: str, page_num: int) -> str:
        q = query.replace(" ", "-")
        if page_num == 1:
            return f"https://listado.mercadolibre.com.co/{q}"
        else:
            offset = (page_num - 1) * 50
            return f"https://listado.mercadolibre.com.co/{q}_Desde_{offset+1}"

    def _get_headers(self, is_mobile: bool = False) -> Dict[str, str]:
        return get_realistic_headers(is_mobile)

    def _deduplicate(self, products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return list({p["id"]: p for p in products}.values())
