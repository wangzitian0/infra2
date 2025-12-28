"""
Global pytest fixtures and configuration for E2E tests.
"""
import os
import asyncio
from typing import AsyncGenerator
from pathlib import Path
import pytest
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Browser, BrowserContext, Page


ROOT = Path(__file__).parent.parent
SERVICE_ROOT = Path(__file__).parent
ENV_NAME = os.getenv("DEPLOY_ENV", "production")

# Load project + environment + service env files (service overrides)
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)
load_dotenv(ROOT / f".env.{ENV_NAME}")
load_dotenv(SERVICE_ROOT / f".env.{ENV_NAME}", override=True)


def _require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Required environment variable '{name}' is not set. Check your env files.")
    return val


class TestConfig:
    """Test configuration from environment variables."""

    # Domain
    BASE_DOMAIN = os.getenv("E2E_DOMAIN") or os.getenv("INTERNAL_DOMAIN") or os.getenv("BASE_DOMAIN") or _require_env("BASE_DOMAIN")

    # Core Bootstrap services
    DOKPLOY_URL = os.getenv("DOKPLOY_URL", f"https://cloud.{BASE_DOMAIN}")
    OP_URL = os.getenv("OP_URL", f"https://op.{BASE_DOMAIN}")
    VAULT_URL = os.getenv("VAULT_URL", f"https://vault.{BASE_DOMAIN}")
    SSO_URL = os.getenv("SSO_URL", f"https://sso.{BASE_DOMAIN}")

    # Optional portal/homepage
    PORTAL_URL = os.getenv("PORTAL_URL", "")

    # Auth (optional, only required for login flows)
    E2E_USERNAME = os.getenv("E2E_USERNAME")
    E2E_PASSWORD = os.getenv("E2E_PASSWORD")

    # Platform DB (optional)
    PLATFORM_DB_HOST = os.getenv("PLATFORM_DB_HOST")
    PLATFORM_DB_PORT = os.getenv("PLATFORM_DB_PORT", "5432")
    PLATFORM_DB_USER = os.getenv("PLATFORM_DB_USER", "postgres")
    PLATFORM_DB_PASSWORD = os.getenv("PLATFORM_DB_PASSWORD") or os.getenv("PG_PASS") or ""

    # Test Configuration (Sensible defaults for execution)
    HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
    TIMEOUT_MS = int(os.getenv("TIMEOUT_MS", "30000"))
    SLOW_MO = int(os.getenv("SLOW_MO", "0"))


@pytest.fixture(scope="session")
def config() -> TestConfig:
    """Provide test configuration."""
    return TestConfig()


@pytest.fixture(scope="session")
def event_loop():
    """Create and set event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def browser() -> AsyncGenerator[Browser, None]:
    """Launch Playwright browser."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=TestConfig.HEADLESS,
            slow_mo=TestConfig.SLOW_MO,
        )
        yield browser
        await browser.close()


@pytest.fixture
async def context(browser: Browser) -> AsyncGenerator[BrowserContext, None]:
    """Create browser context."""
    context = await browser.new_context(
        ignore_https_errors=True,  # Allow self-signed certs
        viewport={"width": 1280, "height": 720},
    )
    yield context
    await context.close()


@pytest.fixture
async def page(context: BrowserContext) -> AsyncGenerator[Page, None]:
    """Create browser page."""
    page = await context.new_page()
    page.set_default_timeout(TestConfig.TIMEOUT_MS)
    yield page
    await page.close()


@pytest.fixture
async def platform_pg_connection():
    """
    Provide a PostgreSQL connection to platform database.

    Automatically closes connection after test.
    Skips test if asyncpg not installed or connection fails.
    """
    try:
        import asyncpg
    except ImportError:
        pytest.skip("asyncpg not installed")

    config = TestConfig()
    if not config.PLATFORM_DB_HOST or not config.PLATFORM_DB_PASSWORD:
        pytest.skip("Platform DB credentials not configured")

    try:
        conn = await asyncpg.connect(
            host=config.PLATFORM_DB_HOST,
            port=int(config.PLATFORM_DB_PORT),
            user=config.PLATFORM_DB_USER,
            password=config.PLATFORM_DB_PASSWORD,
            database="postgres",
            timeout=10.0,
        )
        yield conn
        await conn.close()
    except Exception as e:
        pytest.skip(f"Cannot connect to platform database: {e}")


@pytest.fixture
async def platform_pg_connection_to_db():
    """
    Factory fixture for connecting to specific databases.

    Returns a coroutine that creates connections to named databases.
    """
    try:
        import asyncpg
    except ImportError:
        pytest.skip("asyncpg not installed")

    config = TestConfig()
    if not config.PLATFORM_DB_HOST or not config.PLATFORM_DB_PASSWORD:
        pytest.skip("Platform DB credentials not configured")

    connections = []

    async def connect_to(database: str):
        try:
            conn = await asyncpg.connect(
                host=config.PLATFORM_DB_HOST,
                port=int(config.PLATFORM_DB_PORT),
                user=config.PLATFORM_DB_USER,
                password=config.PLATFORM_DB_PASSWORD,
                database=database,
                timeout=10.0,
            )
            connections.append(conn)
            return conn
        except Exception as e:
            pytest.skip(f"Cannot connect to database: {e}")

    yield connect_to

    for conn in connections:
        await conn.close()
