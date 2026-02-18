# Agent Documentation: Gemini CLI - MercadoLibre Scraper Debugging

## Role and Expertise

I operate as a **Python Senior Developer** with specialized expertise in **Web Scraping Technologies** and **Data Analysis**. My approach is methodical, diagnostic, and focused on delivering robust, maintainable solutions. I leverage state-of-the-art tools and techniques to address complex scraping challenges, particularly those involving anti-bot mechanisms and dynamic content.

As an **Expert Data Analyst**, I prioritize understanding the data's structure, extraction reliability, and the implications of website changes on data integrity. My decisions are guided by an analytical understanding of the problem space, ensuring that fixes are not superficial but address the underlying technical challenges.

## Settings and Mandates

-   **Focus:** Strictly limited to debugging and fixing the `mercadolibre_scraper.py` module. The `amazon_scraper.py` logic remains untouched.
-   **Methodology:** Iterative debugging process:
    1.  **Observe:** Analyze user-provided logs (`scraper.log`) and output.
    2.  **Hypothesize:** Formulate a theory about the root cause.
    3.  **Inspect:** Utilize debugging tools (e.g., `debug_pages` HTML, Playwright's capabilities) to gather concrete evidence.
    4.  **Diagnose:** Pinpoint the exact issue based on inspection.
    5.  **Implement:** Apply precise code modifications.
    6.  **Verify:** Instruct user to re-run and provide feedback/new logs.
-   **Tool Preference:** Prioritize `uv` for project management and environment setup over `pip` when interacting with the user's project files and environment.
-   **Communication:** Precise, clear, and avoids assumptions. If information is ambiguous, I will ask for clarification.
-   **Debugging Philosophy:** When faced with parsing issues, my primary goal is to obtain the *exact* HTML that the scraper is attempting to parse. This is critical for identifying correct CSS selectors. When direct browsing is not possible, I will implement code modifications to capture this HTML for analysis.
-   **Documentation:** Maintain clear internal and external documentation of my process and findings.

## Current Problem Context: MercadoLibre Scraper Failure

The MercadoLibre scraper is consistently failing to extract products, reporting "No products were found." The `scraper.log` indicates that:
-   The API strategy (`Strategy 2`) correctly returns a 403.
-   The HTTPX strategy (`Strategy 3`) fetches a 200 OK but `_is_valid_page` correctly identifies it as a JavaScript challenge page, saving a debug HTML.
-   The Playwright strategy (`Strategy 4`) successfully navigates the URL, and its `wait_for_selector` correctly identifies main product containers (`li.ui-search-layout__item`).
-   Crucially, `_parse_html` then processes these containers but `_extract_product_info` consistently fails to extract *individual product details* (title, URL, price), leading to many `PARSE FAIL: Could not extract 'title'` warnings and ultimately returning 0 products.

**Diagnosis:** The CSS selectors within `_extract_product_info` for `title`, `url`, and `price` are outdated or insufficiently specific for the current MercadoLibre HTML structure within the identified product containers.

**Current State of Fix:**
1.  Attempted initial broad selector updates for `_parse_html` and `_extract_product_info`.
2.  Corrected a `NameError` in `_is_valid_page` related to `body_text` definition.
3.  **Implemented a debug mechanism:** Modified `_fetch_with_playwright` to save the raw HTML received by Playwright (`mercadolibre_playwright_successful_fetch.html`) after successful navigation and container identification. This file is now the primary artifact required for a definitive selector fix.

## Next Steps

1.  **User Action Required:** The user needs to re-run the scraper to generate the `mercadolibre_playwright_successful_fetch.html` debug file.
2.  **Agent Action (Post-rerun):** I will retrieve and analyze `mercadolibre_playwright_successful_fetch.html` to determine the precise, updated CSS selectors for product title, URL, and price.
3.  **Implementation:** Apply these precise selectors to `_extract_product_info`.
4.  **Verification:** Instruct user for final test run.
