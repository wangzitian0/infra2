"""
Global pytest fixtures and configuration for E2E tests.
"""
import os
import sys
import asyncio
from pathlib import Path
from typing import AsyncGenerator
import pytest
import httpx
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

# Ensure repo root is on sys.path for libs imports when running from e2e_regressions/.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(
            f"Required environment variable '{name}' is not set. "
            "Set it in your shell/CI (see .env.example for the key list)."
        )
    return val


def _load_init_vars() -> dict[str, str]:
    """Load init/env_vars from 1Password when env vars are missing."""
    try:
        from libs.env import OpSecrets
    except Exception:
        return {}
    try:
        return OpSecrets().get_all() or {}
    except Exception:
        return {}


def _resolve_internal_domain() -> str:
    """Resolve INTERNAL_DOMAIN with optional 1Password fallback."""
    env_domain = os.getenv("INTERNAL_DOMAIN")
    if env_domain:
        return env_domain
    init_vars = _load_init_vars()
    return init_vars.get("INTERNAL_DOMAIN") or _require_env("INTERNAL_DOMAIN")


class TestConfig:
    """Test configuration from environment variables.

    Required: INTERNAL_DOMAIN.
    Optional: E2E_USERNAME/E2E_PASSWORD (auth flows), PORTAL_URL, DB creds.
    """
    # Import SERVICE_SUBDOMAINS for canonical subdomain mapping
    from libs.common import SERVICE_SUBDOMAINS

    # Domain
    INTERNAL_DOMAIN = _resolve_internal_domain()

    # Generate URLs from SERVICE_SUBDOMAINS (single source of truth)
    DOKPLOY_URL = os.getenv("DOKPLOY_URL", f"https://{SERVICE_SUBDOMAINS['dokploy']}.{INTERNAL_DOMAIN}")
    OP_URL = os.getenv("OP_URL", f"https://{SERVICE_SUBDOMAINS['1password']}.{INTERNAL_DOMAIN}")
    VAULT_URL = os.getenv("VAULT_URL", f"https://{SERVICE_SUBDOMAINS['vault']}.{INTERNAL_DOMAIN}")
    SSO_URL = os.getenv("SSO_URL", f"https://{SERVICE_SUBDOMAINS['sso']}.{INTERNAL_DOMAIN}")

    # MinIO Object Storage
    MINIO_CONSOLE_URL = os.getenv("MINIO_CONSOLE_URL", f"https://{SERVICE_SUBDOMAINS['minio_console']}.{INTERNAL_DOMAIN}")
    MINIO_API_URL = os.getenv("MINIO_API_URL", f"https://{SERVICE_SUBDOMAINS['minio_api']}.{INTERNAL_DOMAIN}")

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
def repo_root() -> Path:
    """Return the repository root path."""
    return ROOT


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


@pytest.fixture
async def http_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Shared HTTP client for platform tests."""
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        yield client
