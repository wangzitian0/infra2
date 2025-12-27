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


# Load environment variables
env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)



def get_env_required(name: str) -> str:
    """Get environment variable or raise error."""
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Required environment variable '{name}' is not set. Check your CI/local .env file.")
    return val


class TestConfig:
    """Test configuration from environment variables."""

    # Domain - E2E_DOMAIN (preferred) or fallback to INTERNAL_DOMAIN/BASE_DOMAIN
    BASE_DOMAIN = os.getenv("E2E_DOMAIN") or os.getenv("INTERNAL_DOMAIN") or get_env_required("BASE_DOMAIN")

    # Portal & SSO
    PORTAL_URL = os.getenv("PORTAL_URL", f"https://home.{BASE_DOMAIN}")
    SSO_URL = os.getenv("SSO_URL", f"https://sso.{BASE_DOMAIN}")
    
    # Credentials - E2E_USERNAME/E2E_PASSWORD (preferred) or fallback
    E2E_USERNAME = os.getenv("E2E_USERNAME", "admin")
    E2E_PASSWORD = get_env_required("E2E_PASSWORD")

    # Platform Services
    VAULT_URL = os.getenv("VAULT_URL", f"https://secrets.{BASE_DOMAIN}")
    DASHBOARD_URL = os.getenv("DASHBOARD_URL", f"https://kdashboard.{BASE_DOMAIN}")
    DIGGER_URL = os.getenv("DIGGER_URL", f"https://digger.{BASE_DOMAIN}")
    KUBERO_URL = os.getenv("KUBERO_URL", f"https://kcloud.{BASE_DOMAIN}")
    SIGNOZ_URL = os.getenv("SIGNOZ_URL", f"https://signoz.{BASE_DOMAIN}")
    K3S_URL = os.getenv("K3S_URL", f"https://k3s.{BASE_DOMAIN}:6443")

    # K8s Resource Identifiers
    class K8sResources:
        """Kubernetes resource naming and configuration constants."""
        # Platform PostgreSQL
        PLATFORM_PG_NAME = "platform-pg"
        PLATFORM_PG_NAMESPACE = "platform"
        CNPG_CLUSTER_TYPE = "clusters.postgresql.cnpg.io"
        PLATFORM_PG_LABEL = "cnpg.io/cluster=platform-pg"
        
        # Namespaces
        CRITICAL_NAMESPACES = ["kube-system", "bootstrap", "platform"]
        
        # Pod Health Thresholds
        MIN_SYSTEM_POD_HEALTH_RATIO = 0.8  # 80%
        MIN_PLATFORM_POD_HEALTH_RATIO = 0.5  # 50% (allow for initializing pods)
        MIN_BOOTSTRAP_POD_COUNT = 1
        
        # Token/Secret Length Requirements
        MIN_TOKEN_LENGTH = 32
        MIN_WEBHOOK_SECRET_LENGTH = 16
        
        # Digger Configuration
        DIGGER_POD_LABEL = "app.kubernetes.io/name=digger-backend"
        DIGGER_NAMESPACE = "bootstrap"
        
        # Traefik Configuration
        TRAEFIK_NAMESPACE = "kube-system"
        TRAEFIK_LABELS = [
            "app.kubernetes.io/name=traefik",
            "app=traefik"
        ]
    
    # Database Configuration
    PLATFORM_DB_HOST = os.getenv("PLATFORM_DB_HOST", f"platform-pg-rw.platform.svc.cluster.local")
    PLATFORM_DB_PORT = os.getenv("PLATFORM_DB_PORT", "5432")
    PLATFORM_DB_USER = os.getenv("PLATFORM_DB_USER", "postgres")
    PLATFORM_DB_PASSWORD = os.getenv("PLATFORM_DB_PASSWORD") or os.getenv("TF_VAR_vault_postgres_password", "")

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
    Provide a PostgreSQL connection to platform-pg database.
    
    Automatically closes connection after test.
    Skips test if asyncpg not installed or connection fails.
    """
    try:
        import asyncpg
    except ImportError:
        pytest.skip("asyncpg not installed")
    
    config = TestConfig()
    if not config.PLATFORM_DB_PASSWORD:
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
    if not config.PLATFORM_DB_PASSWORD:
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
            pytest.skip(f"Cannot connect to database {database}: {e}")
    
    yield connect_to
    
    # Cleanup: close all connections
    for conn in connections:
        await conn.close()


# Markers
def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "smoke: quick smoke tests")
    config.addinivalue_line("markers", "bootstrap: bootstrap layer tests")
    config.addinivalue_line("markers", "sso: SSO/Portal tests")
    config.addinivalue_line("markers", "platform: Platform service tests")
    config.addinivalue_line("markers", "api: API endpoint tests")
    config.addinivalue_line("markers", "e2e: full end-to-end tests")
    config.addinivalue_line("markers", "compute: compute layer tests")
    config.addinivalue_line("markers", "storage: storage layer tests")
    config.addinivalue_line("markers", "network: network layer tests")
