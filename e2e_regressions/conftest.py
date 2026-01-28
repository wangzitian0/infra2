"""
Global pytest fixtures and configuration for E2E tests.
"""

import os
import sys
import asyncio
from pathlib import Path
from typing import AsyncGenerator
from urllib.parse import urlparse

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


def _resolve_base_domain() -> str:
    """Resolve BASE_DOMAIN with optional 1Password fallback, defaulting to INTERNAL_DOMAIN."""
    env_domain = os.getenv("BASE_DOMAIN")
    if env_domain:
        return env_domain
    init_vars = _load_init_vars()
    return init_vars.get("BASE_DOMAIN") or _resolve_internal_domain()


def _normalize_env_suffix(deploy_env: str) -> str:
    if deploy_env == "production":
        return ""
    return f"-{deploy_env}"


def _build_app_base_domain(base_domain: str, env_suffix: str) -> str:
    if not env_suffix:
        return base_domain
    return f"{env_suffix.lstrip('-')}.{base_domain}"


def _build_env_context() -> dict[str, str]:
    deploy_env = os.getenv("DEPLOY_ENV", "production").lower().strip()
    if deploy_env not in {"production", "staging", "pr-test"}:
        raise RuntimeError(
            "DEPLOY_ENV must be one of: production, staging, pr-test. "
            f"Got: {deploy_env}"
        )

    pr_number = os.getenv("PR_NUMBER", "").strip()
    if deploy_env == "pr-test" and not pr_number:
        raise RuntimeError("PR_NUMBER is required when DEPLOY_ENV=pr-test")

    env_suffix = _normalize_env_suffix(deploy_env)
    if deploy_env == "pr-test":
        env_suffix = f"-pr-{pr_number}"

    base_domain = _resolve_base_domain()
    app_base_domain = _build_app_base_domain(base_domain, env_suffix)
    return {
        "deploy_env": deploy_env,
        "env_suffix": env_suffix,
        "base_domain": base_domain,
        "app_base_domain": app_base_domain,
        "pr_number": pr_number,
    }


def _validate_url(name: str, url: str) -> None:
    if not url:
        raise RuntimeError(f"{name} is required but empty")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(f"{name} must be a valid http(s) URL. Got: {url}")


class TestConfig:
    """Test configuration from environment variables.

    Required: INTERNAL_DOMAIN (env or 1Password fallback).
    Optional: DEPLOY_ENV (defaults to production), BASE_DOMAIN (app domain override),
    PR_NUMBER when DEPLOY_ENV=pr-test, E2E_USERNAME/E2E_PASSWORD, PORTAL_URL, DB creds.
    """

    # Import SERVICE_SUBDOMAINS for canonical subdomain mapping
    from libs.common import SERVICE_SUBDOMAINS

    ENV_CONTEXT = _build_env_context()
    DEPLOY_ENV = ENV_CONTEXT["deploy_env"]
    ENV_SUFFIX = ENV_CONTEXT["env_suffix"]
    BASE_DOMAIN = ENV_CONTEXT["base_domain"]
    APP_BASE_DOMAIN = ENV_CONTEXT["app_base_domain"]
    PR_NUMBER = ENV_CONTEXT["pr_number"]

    # Domain
    INTERNAL_DOMAIN = _resolve_internal_domain()

    # Generate URLs from SERVICE_SUBDOMAINS (single source of truth)
    DOKPLOY_URL = os.getenv(
        "DOKPLOY_URL",
        f"https://{SERVICE_SUBDOMAINS['dokploy']}{ENV_SUFFIX}.{INTERNAL_DOMAIN}",
    )
    OP_URL = os.getenv(
        "OP_URL",
        f"https://{SERVICE_SUBDOMAINS['1password']}{ENV_SUFFIX}.{INTERNAL_DOMAIN}",
    )
    VAULT_URL = os.getenv(
        "VAULT_URL",
        f"https://{SERVICE_SUBDOMAINS['vault']}{ENV_SUFFIX}.{INTERNAL_DOMAIN}",
    )
    SSO_URL = os.getenv(
        "SSO_URL",
        f"https://{SERVICE_SUBDOMAINS['sso']}{ENV_SUFFIX}.{INTERNAL_DOMAIN}",
    )

    # MinIO Object Storage
    MINIO_CONSOLE_URL = os.getenv(
        "MINIO_CONSOLE_URL",
        f"https://{SERVICE_SUBDOMAINS['minio_console']}{ENV_SUFFIX}.{INTERNAL_DOMAIN}",
    )
    MINIO_API_URL = os.getenv(
        "MINIO_API_URL",
        f"https://{SERVICE_SUBDOMAINS['minio_api']}{ENV_SUFFIX}.{INTERNAL_DOMAIN}",
    )

    # App domain (e.g., report.zitian.party, report-pr-47.zitian.party, report-staging.zitian.party)
    FINANCE_REPORT_BASE = os.getenv(
        "FINANCE_REPORT_BASE",
        f"report{ENV_SUFFIX}.{INTERNAL_DOMAIN}",
    )
    FINANCE_REPORT_URL = os.getenv(
        "FINANCE_REPORT_URL", f"https://{FINANCE_REPORT_BASE}"
    )
    FINANCE_REPORT_API_URL = os.getenv(
        "FINANCE_REPORT_API_URL",
        f"https://{FINANCE_REPORT_BASE}/api",
    )

    # Optional portal/homepage
    PORTAL_URL = os.getenv("PORTAL_URL", "")

    # Auth (optional, only required for login flows)
    E2E_USERNAME = os.getenv("E2E_USERNAME")
    E2E_PASSWORD = os.getenv("E2E_PASSWORD")

    # Platform DB (optional)
    PLATFORM_DB_HOST = os.getenv("PLATFORM_DB_HOST") or os.getenv("DB_HOST")
    PLATFORM_DB_PORT = os.getenv("PLATFORM_DB_PORT") or os.getenv("DB_PORT", "5432")
    PLATFORM_DB_USER = os.getenv("PLATFORM_DB_USER") or os.getenv("DB_USER", "postgres")
    PLATFORM_DB_PASSWORD = (
        os.getenv("PLATFORM_DB_PASSWORD")
        or os.getenv("DB_PASSWORD")
        or os.getenv("PG_PASS")
        or ""
    )

    # Test Configuration (Sensible defaults for execution)
    HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
    TIMEOUT_MS = int(os.getenv("TIMEOUT_MS", "30000"))
    SLOW_MO = int(os.getenv("SLOW_MO", "0"))

    @classmethod
    def validate(cls) -> None:
        allow_custom_domain = (
            os.getenv("E2E_ALLOW_CUSTOM_DOMAIN", "false").lower() == "true"
        )
        required_urls = {
            "DOKPLOY_URL": cls.DOKPLOY_URL,
            "VAULT_URL": cls.VAULT_URL,
            "SSO_URL": cls.SSO_URL,
            "OP_URL": cls.OP_URL,
            "MINIO_CONSOLE_URL": cls.MINIO_CONSOLE_URL,
            "MINIO_API_URL": cls.MINIO_API_URL,
            "FINANCE_REPORT_URL": cls.FINANCE_REPORT_URL,
            "FINANCE_REPORT_API_URL": cls.FINANCE_REPORT_API_URL,
        }
        for name, url in required_urls.items():
            _validate_url(name, url)

        if cls.DEPLOY_ENV == "pr-test" and not cls.PR_NUMBER:
            raise RuntimeError("PR_NUMBER must be set when DEPLOY_ENV=pr-test")

        finance_host = urlparse(cls.FINANCE_REPORT_URL).hostname
        api_host = urlparse(cls.FINANCE_REPORT_API_URL).hostname
        api_path = urlparse(cls.FINANCE_REPORT_API_URL).path.rstrip("/")
        if not finance_host or not api_host:
            raise RuntimeError(
                "FINANCE_REPORT_URL and FINANCE_REPORT_API_URL must include hosts"
            )
        if finance_host != api_host:
            raise RuntimeError(
                "FINANCE_REPORT_URL and FINANCE_REPORT_API_URL must share the same host"
            )
        if api_path != "/api":
            raise RuntimeError("FINANCE_REPORT_API_URL must end with /api")

        if not allow_custom_domain:
            expected_domains = {
                "DOKPLOY_URL": f"{cls.SERVICE_SUBDOMAINS['dokploy']}{cls.ENV_SUFFIX}.{cls.INTERNAL_DOMAIN}",
                "OP_URL": f"{cls.SERVICE_SUBDOMAINS['1password']}{cls.ENV_SUFFIX}.{cls.INTERNAL_DOMAIN}",
                "VAULT_URL": f"{cls.SERVICE_SUBDOMAINS['vault']}{cls.ENV_SUFFIX}.{cls.INTERNAL_DOMAIN}",
                "SSO_URL": f"{cls.SERVICE_SUBDOMAINS['sso']}{cls.ENV_SUFFIX}.{cls.INTERNAL_DOMAIN}",
                "MINIO_CONSOLE_URL": f"{cls.SERVICE_SUBDOMAINS['minio_console']}{cls.ENV_SUFFIX}.{cls.INTERNAL_DOMAIN}",
                "MINIO_API_URL": f"{cls.SERVICE_SUBDOMAINS['minio_api']}{cls.ENV_SUFFIX}.{cls.INTERNAL_DOMAIN}",
                "FINANCE_REPORT_URL": f"report{cls.ENV_SUFFIX}.{cls.INTERNAL_DOMAIN}",
            }
            for name, expected_host in expected_domains.items():
                actual_host = urlparse(required_urls[name]).hostname
                if actual_host != expected_host:
                    raise RuntimeError(
                        f"{name} host mismatch. Expected {expected_host}, got {actual_host}. "
                        "Set E2E_ALLOW_CUSTOM_DOMAIN=true to override."
                    )

        if cls.PORTAL_URL:
            _validate_url("PORTAL_URL", cls.PORTAL_URL)


@pytest.fixture(scope="session")
def config() -> TestConfig:
    """Provide test configuration."""
    TestConfig.validate()
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
            timeout=10,
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
                timeout=10,
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
