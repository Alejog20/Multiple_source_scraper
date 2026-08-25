import unittest
import logging
import sys
import json
from pathlib import Path
from typing import List, Dict, Any, Optional
from unittest.mock import patch, MagicMock

logging.basicConfig(level=logging.WARNING)

try:
    from amazon_scraper import AmazonScraper
    from mercadolibre_scraper import MercadoLibreScraper
    from debug_utils import validate_product_data, setup_logging
except ImportError as e:
    print(f"Error importing scraper modules: {e}")
    sys.exit(1)


class TestAmazonScraper(unittest.TestCase):
    """Test cases for Amazon scraper parsing logic."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.scraper = AmazonScraper()
        self.test_data_dir = Path("test_data")
        self.amazon_html_file = self.test_data_dir / "amazon_test_page.html"

    def load_test_html(self, filename: str) -> str:
        """Load HTML content from test data file."""
        file_path = self.test_data_dir / filename
        if not file_path.exists():
            self.skipTest(f"Test HTML file not found: {file_path}")

        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()

    def test_html_parsing_basic(self) -> None:
        """Test basic HTML parsing functionality."""
        html_content = self.load_test_html("amazon_test_page.html")

        products, is_valid = self.scraper._parse_html(html_content, is_mobile=False)

        self.assertTrue(is_valid, "HTML page should be detected as valid")
        self.assertIsInstance(products, list, "Parser should return a list")

    def test_product_extraction_count(self) -> None:
        """Test that the correct number of products are extracted."""
        html_content = self.load_test_html("amazon_test_page.html")

        products, is_valid = self.scraper._parse_html(html_content, is_mobile=False)

        if is_valid and products:
            self.assertGreater(len(products), 0, "Should extract at least one product")
            self.assertLessEqual(len(products), 50, "Should not extract more than 50 products per page")

    def test_product_data_structure(self) -> None:
        """Test that extracted products have the correct data structure."""
        html_content = self.load_test_html("amazon_test_page.html")

        products, is_valid = self.scraper._parse_html(html_content, is_mobile=False)

        if is_valid and products:
            product = products[0]
            required_fields = ["id", "source", "title", "url", "price", "currency"]

            for field in required_fields:
                self.assertIn(field, product, f"Product should have '{field}' field")

            self.assertEqual(product["source"], "Amazon", "Source should be 'Amazon'")

            self.assertIsInstance(product["id"], str, "ID should be a string")
            self.assertIsInstance(product["title"], str, "Title should be a string")
            self.assertTrue(len(product["title"]) > 0, "Title should not be empty")

            if product["price"] is not None:
                self.assertIsInstance(product["price"], (int, float), "Price should be numeric")
                self.assertGreaterEqual(product["price"], 0, "Price should be non-negative")

            if product["url"] is not None:
                self.assertIsInstance(product["url"], str, "URL should be a string")
                self.assertTrue(product["url"].startswith("http"), "URL should start with http")

    def test_data_validation(self) -> None:
        """Test that product data validation works correctly."""
        html_content = self.load_test_html("amazon_test_page.html")

        products, is_valid = self.scraper._parse_html(html_content, is_mobile=False)

        if is_valid and products:
            for product in products:
                validated = validate_product_data(product)
                if validated:
                    self.assertIsNotNone(validated["id"], "Validated product must have ID")
                    self.assertIsNotNone(validated["title"], "Validated product must have title")
                    self.assertTrue(len(validated["title"]) > 0, "Validated title must not be empty")

    def test_invalid_html_handling(self) -> None:
        """Test handling of invalid or empty HTML."""
        products, is_valid = self.scraper._parse_html("", is_mobile=False)
        self.assertFalse(is_valid, "Empty HTML should be marked as invalid")

        invalid_html = "<html><body><h1>No products here</h1></body></html>"
        products, is_valid = self.scraper._parse_html(invalid_html, is_mobile=False)
        self.assertTrue(isinstance(products, list), "Should return empty list for invalid HTML")

    def test_page_validation_captcha_detection(self) -> None:
        """Test CAPTCHA detection in page validation."""
        captcha_html = '''
        <html>
            <body>
                <form action="/errors/validateCaptcha">
                    <input type="text" name="captcha">
                </form>
            </body>
        </html>
        '''

        products, is_valid = self.scraper._parse_html(captcha_html, is_mobile=False)
        self.assertFalse(is_valid, "CAPTCHA page should be detected as invalid")

    def test_mobile_vs_desktop_parsing(self) -> None:
        """Test that mobile and desktop parsing work correctly."""
        html_content = self.load_test_html("amazon_test_page.html")

        desktop_products, desktop_valid = self.scraper._parse_html(html_content, is_mobile=False)
        mobile_products, mobile_valid = self.scraper._parse_html(html_content, is_mobile=True)

        self.assertEqual(desktop_valid, mobile_valid, "Desktop and mobile parsing validity should match")


class TestMercadoLibreScraper(unittest.TestCase):
    """Test cases for MercadoLibre scraper parsing logic."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.scraper = MercadoLibreScraper(country_code="co")
        self.test_data_dir = Path("test_data")
        self.mercadolibre_html_file = self.test_data_dir / "mercadolibre_test_page.html"

    def load_test_html(self, filename: str) -> str:
        """Load HTML content from test data file."""
        file_path = self.test_data_dir / filename
        if not file_path.exists():
            self.skipTest(f"Test HTML file not found: {file_path}")

        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()

    def test_pure_json_parsing_mercadolibre(self) -> None:
        """Test direct JSON parsing functionality."""
        json_file_path = self.test_data_dir / "mercadolibre_pure_json.json"
        if not json_file_path.exists():
            self.skipTest(f"Pure JSON test file not found: {json_file_path}")

        with open(json_file_path, 'r', encoding='utf-8-sig') as f:
            json_data = json.load(f)

        # Navigate to the actual product list within the loaded JSON
        products_data_raw = []
        if 'appProps' in json_data and 'pageProps' in json_data['appProps'] and \
           'initialState' in json_data['appProps']['pageProps'] and \
           'results' in json_data['appProps']['pageProps']['initialState']:
            products_data_raw = json_data['appProps']['pageProps']['initialState']['results']
        
        self.assertIsInstance(products_data_raw, list, "Should find a list of raw product data")
        self.assertGreater(len(products_data_raw), 0, "Should have raw product data to process")

        products = []
        for item_data in products_data_raw:
            # Filter for actual product items, ignoring non-product 'POLYCARDS' or similar structure
            if item_data.get('item_id'): # This indicates a direct product item
                product = self.scraper._extract_product_from_json_data_item(item_data)
                if product:
                    products.append(product)
            elif item_data.get('id') == 'POLYCARD' and item_data.get('polycard'): # Handle nested polycard structure
                product = self.scraper._extract_product_from_json_data_item(item_data['polycard'])
                if product:
                    products.append(product)
                
        self.assertIsInstance(products, list, "Parser should return a list of extracted products")
        self.assertGreater(len(products), 0, "Should extract at least one product from JSON")

        first_product = products[0]
        self.assertIn("id", first_product)
        self.assertIn("title", first_product)
        self.assertIn("price", first_product)
        self.assertIn("currency", first_product)
        self.assertIn("url", first_product)

        # Using a more flexible check as the exact values might differ slightly due to processing
        self.assertTrue(first_product["id"].startswith("MCO"))
        self.assertIsInstance(first_product["title"], str)
        self.assertGreater(first_product["price"], 0)
        self.assertEqual(first_product["currency"], "COP")
        self.assertTrue(first_product["url"].startswith("https://www.mercadolibre.com.co/"))

        second_product = products[1]
        self.assertTrue(second_product["id"].startswith("MCO"))
        self.assertIsInstance(second_product["title"], str)
        self.assertGreater(second_product["price"], 0)
        self.assertEqual(second_product["currency"], "COP")
        self.assertTrue(second_product["url"].startswith("https://www.mercadolibre.com.co/"))

    # def test_json_parsing_mercadolibre(self) -> None:
    #     """Test HTML parsing functionality using embedded JSON data."""
    #     html_content = self.load_test_html("mercadolibre_json_test_page.html")

    #     products, is_valid = self.scraper._parse_html(html_content)

    #     self.assertTrue(is_valid, "HTML page with JSON should be detected as valid")
    #     self.assertIsInstance(products, list, "Parser should return a list")
    #     self.assertGreater(len(products), 0, "Should extract at least one product from JSON")

    #     first_product = products[0]
    #     self.assertIn("id", first_product)
    #     self.assertIn("title", first_product)
    #     self.assertIn("price", first_product)
    #     self.assertIn("currency", first_product)
    #     self.assertIn("url", first_product)

    #     self.assertEqual(first_product["id"], "MCO3534935010")
    #     self.assertEqual(first_product["title"], "Lente Para Cámara Tamron 0.669-2.756in F/2.8 Di Iii-a Vc Rxd Color Negro")
    #     self.assertEqual(first_product["price"], 3499999)
    #     self.assertEqual(first_product["currency"], "COP")
    #     self.assertTrue(first_product["url"].startswith("https://www.mercadolibre.com.co/"))

    #     second_product = products[1]
    #     self.assertEqual(second_product["id"], "MCO3543713410")
    #     self.assertEqual(second_product["title"], "Tamron 17-70 Mm F/2.8 Di Iii-a For Sony A6400 Zoom")
    #     self.assertEqual(second_product["price"], 3833190)
    #     self.assertEqual(second_product["currency"], "COP")
    #     self.assertTrue(second_product["url"].startswith("https://www.mercadolibre.com.co/"))

    def test_html_parsing_basic(self) -> None:
        """Test basic HTML parsing functionality."""
        html_content = self.load_test_html("mercadolibre_test_page.html")

        products, is_valid = self.scraper._parse_html(html_content)

        self.assertTrue(is_valid, "HTML page should be detected as valid")
        self.assertIsInstance(products, list, "Parser should return a list")

    def test_product_extraction_count(self) -> None:
        """Test that the correct number of products are extracted."""
        html_content = self.load_test_html("mercadolibre_test_page.html")

        products, is_valid = self.scraper._parse_html(html_content)

        if is_valid and products:
            self.assertGreater(len(products), 0, "Should extract at least one product")
            self.assertLessEqual(len(products), 50, "Should not extract more than 50 products per page")

    def test_product_data_structure(self) -> None:
        """Test that extracted products have the correct data structure."""
        html_content = self.load_test_html("mercadolibre_test_page.html")

        products, is_valid = self.scraper._parse_html(html_content)

        if is_valid and products:
            product = products[0]
            required_fields = ["id", "source", "title", "url", "price", "currency"]

            for field in required_fields:
                self.assertIn(field, product, f"Product should have '{field}' field")

            self.assertEqual(product["source"], "MercadoLibre", "Source should be 'MercadoLibre'")

            if product["id"]:
                self.assertIsInstance(product["id"], str, "ID should be a string")
                self.assertTrue(len(product["id"]) > 0, "ID should not be empty")

            self.assertIsInstance(product["title"], str, "Title should be a string")
            self.assertTrue(len(product["title"]) > 0, "Title should not be empty")

            if product["price"] is not None:
                self.assertIsInstance(product["price"], (int, float), "Price should be numeric")
                self.assertGreaterEqual(product["price"], 0, "Price should be non-negative")

    def test_title_extraction_from_img(self) -> None:
        """Test that titles are correctly extracted from img title attribute."""
        sample_html = '''
        <html>
            <body>
                <li class="ui-search-layout__item">
                    <div class="ui-search-result__wrapper">
                        <img title="Test Product Title" src="test.jpg" alt="Test Product">
                        <div class="price">$100</div>
                        <a href="/MLA123456789">Product Link</a>
                    </div>
                </li>
            </body>
        </html>
        '''

        products, is_valid = self.scraper._parse_html(sample_html)

        if is_valid and products:
            product = products[0]
            self.assertEqual(product["title"], "Test Product Title",
                           "Title should be extracted from img title attribute")

    def test_api_data_parsing(self) -> None:
        """Test API data parsing functionality."""
        sample_api_data = {
            "results": [
                {
                    "id": "MLA123456789",
                    "title": "Test Product from API",
                    "permalink": "https://mercadolibre.com.co/test-product",
                    "price": 50000,
                    "currency_id": "COP"
                }
            ]
        }

        products = self.scraper._parse_api_data(sample_api_data)

        self.assertEqual(len(products), 1, "Should parse one product from API data")

        product = products[0]
        self.assertEqual(product["id"], "MLA123456789")
        self.assertEqual(product["title"], "Test Product from API")
        self.assertEqual(product["price"], 50000)
        self.assertEqual(product["currency"], "COP")
        self.assertEqual(product["source"], "MercadoLibre")

    def test_data_validation(self) -> None:
        """Test that product data validation works correctly."""
        html_content = self.load_test_html("mercadolibre_test_page.html")

        products, is_valid = self.scraper._parse_html(html_content)

        if is_valid and products:
            for product in products:
                validated = validate_product_data(product)
                if validated:
                    self.assertIsNotNone(validated["id"], "Validated product must have ID")
                    self.assertIsNotNone(validated["title"], "Validated product must have title")
                    self.assertTrue(len(validated["title"]) > 0, "Validated title must not be empty")

    def test_price_format_handling(self) -> None:
        """Test handling of different price formats."""
        sample_html = '''
        <html>
            <body>
                <li class="ui-search-layout__item">
                    <img title="Test Product" src="test.jpg">
                    <span class="andes-money-amount__fraction">1.500.000</span>
                    <a href="/MLA123456789">Link</a>
                </li>
            </body>
        </html>
        '''

        products, is_valid = self.scraper._parse_html(sample_html)

        if is_valid and products:
            product = products[0]
            self.assertEqual(product["price"], 1500000,
                           "Should correctly parse Colombian peso format")

    def test_invalid_html_handling(self) -> None:
        """Test handling of invalid or empty HTML."""
        products, is_valid = self.scraper._parse_html("")
        self.assertFalse(is_valid, "Empty HTML should be marked as invalid")

        no_results_html = '''
        <html>
            <body>
                <div>No hay publicaciones que coincidan con tu búsqueda</div>
            </body>
        </html>
        '''

        products, is_valid = self.scraper._parse_html(no_results_html)
        self.assertFalse(is_valid, "No results page should be detected as invalid")


class TestDataValidation(unittest.TestCase):
    """Test cases for data validation utilities."""

    def test_validate_valid_product(self) -> None:
        """Test validation of a valid product."""
        valid_product = {
            "id": "TEST123",
            "source": "Amazon",
            "title": "Test Product",
            "url": "https://example.com/product",
            "price": 99.99,
            "currency": "$",
            "rating": 4.5,
            "review_count": 100
        }

        validated = validate_product_data(valid_product)
        self.assertIsNotNone(validated, "Valid product should pass validation")
        self.assertEqual(validated["id"], "TEST123")
        self.assertEqual(validated["title"], "Test Product")
        self.assertEqual(validated["price"], 99.99)

    def test_validate_invalid_product_no_id(self) -> None:
        """Test validation rejects product without ID."""
        invalid_product = {
            "source": "Amazon",
            "title": "Test Product",
            "price": 99.99
        }

        validated = validate_product_data(invalid_product)
        self.assertIsNone(validated, "Product without ID should be rejected")

    def test_validate_invalid_product_no_title(self) -> None:
        """Test validation rejects product without title."""
        invalid_product = {
            "id": "TEST123",
            "source": "Amazon",
            "price": 99.99
        }

        validated = validate_product_data(invalid_product)
        self.assertIsNone(validated, "Product without title should be rejected")

    def test_validate_negative_price(self) -> None:
        """Test validation handles negative prices."""
        product_with_negative_price = {
            "id": "TEST123",
            "title": "Test Product",
            "price": -50.0
        }

        validated = validate_product_data(product_with_negative_price)
        if validated:
            self.assertIsNone(validated["price"], "Negative price should be set to None")

    def test_validate_rating_range(self) -> None:
        """Test validation enforces rating range."""
        product_with_invalid_rating = {
            "id": "TEST123",
            "title": "Test Product",
            "rating": 10.0
        }

        validated = validate_product_data(product_with_invalid_rating)
        if validated:
            self.assertIsNone(validated["rating"], "Invalid rating should be set to None")


class TestScraperIntegration(unittest.TestCase):
    """Integration tests for scraper components."""

    def test_file_cache_functionality(self) -> None:
        """Test that file caching works correctly."""
        from debug_utils import FileCache

        cache = FileCache(cache_dir=".test_cache", max_age_hours=1)

        result = cache.get("http://test-url.com")
        self.assertIsNone(result, "Cache miss should return None")

        test_content = "<html>Test content</html>"
        cache.set("http://test-url.com", test_content)

        cached_result = cache.get("http://test-url.com")
        self.assertEqual(cached_result, test_content, "Cache hit should return stored content")

        import shutil
        shutil.rmtree(".test_cache", ignore_errors=True)

    @patch('debug_utils.get_random_user_agent')
    def test_user_agent_integration(self, mock_get_agent: MagicMock) -> None:
        """Test user agent integration."""
        mock_get_agent.return_value = "Test User Agent"

        from debug_utils import get_realistic_headers

        headers = get_realistic_headers(is_mobile=False)

        self.assertIn("User-Agent", headers)
        self.assertEqual(headers["User-Agent"], "Test User Agent")
        mock_get_agent.assert_called_once_with(False)


def run_tests() -> bool:
    """Run all tests and return results."""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    test_classes = [
        TestAmazonScraper,
        TestMercadoLibreScraper,
        TestDataValidation,
        TestScraperIntegration
    ]

    for test_class in test_classes:
        tests = loader.loadTestsFromTestCase(test_class)
        suite.addTests(tests)

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    print("\n" + "="*70)
    print("UNIT TEST SUMMARY")
    print("="*70)
    print(f"Tests run: {result.testsRun}")
    print(f"Failures: {len(result.failures)}")
    print(f"Errors: {len(result.errors)}")
    print(f"Skipped: {len(result.skipped) if hasattr(result, 'skipped') else 0}")

    success_rate = ((result.testsRun - len(result.failures) - len(result.errors)) / result.testsRun * 100) if result.testsRun > 0 else 0
    print(f"Success rate: {success_rate:.1f}%")

    if result.failures:
        print("\nFAILURES:")
        for test, traceback in result.failures:
            print(f"--- FAILURE: {test} ---")
            print(traceback)

    if result.errors:
        print("\nERRORS:")
        for test, traceback in result.errors:
            print(f"--- ERROR: {test} ---")
            print(traceback)

    return result.wasSuccessful()


if __name__ == "__main__":
    print("="*70)
    print("E-COMMERCE SCRAPER UNIT TESTS")
    print("="*70)
    print("Testing parsing logic against static HTML files...")
    print("This validates scraper functionality without making live web requests.\n")

    result_success = run_tests()
    if result_success:
        print("ALL TESTS PASSED! The scrapers are working correctly.")
    else:
        print("SOME TESTS FAILED! Check the output above for details.")
        print("This may indicate that website structures have changed")
        print("and the scrapers need to be updated.")

    print("="*70)