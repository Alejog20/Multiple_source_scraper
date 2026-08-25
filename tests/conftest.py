"""tests/conftest.py — Shared pytest fixtures and test-session bootstrap.

Environment variables that gate module-level state (DATABASE_URL,
TELEGRAM_BOT_TOKEN) must be set here, at import time, before pytest imports
any test module. Both database.py and bot.py read these at *import* time to
build a module-level engine / Bot instance, so setting them inside a fixture
would be too late — the real scraper_jobs.db or a missing-token crash would
already have happened.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Isolated, disposable sqlite file so tests never touch the real
# scraper_jobs.db. A temp *file* (not ":memory:") is required: SQLAlchemy's
# async engine pools multiple connections, and an in-memory sqlite DB is
# per-connection — different pooled connections would each see an empty DB.
_TEST_DB_DIR = tempfile.mkdtemp(prefix="scraper_test_db_")
os.environ.setdefault(
    "DATABASE_URL", f"sqlite+aiosqlite:///{_TEST_DB_DIR}/test.db"
)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST-TOKEN")


@pytest.fixture(autouse=True)
def _isolate_cwd_side_effects(tmp_path, monkeypatch):
    """Run every test from a throwaway directory.

    Parsing invalid/partial HTML in the scraper tests legitimately triggers
    debug_utils.save_debug_html(), which writes into ./debug_pages relative
    to the CWD. Without this, a full test run litters the real project's
    debug_pages/ with dozens of artifacts on every invocation. Module
    imports already happened before this fixture runs, so relocating CWD
    here doesn't affect anything resolved at import time (e.g. scraper.log).
    """
    monkeypatch.chdir(tmp_path)
