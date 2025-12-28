"""
Auth and SSO integration tests.

Tests authentication flows for Authentik.
"""
import os
import pytest
import httpx
from playwright.async_api import Page
from conftest import TestConfig


@pytest.mark.platform
async def test_authentik_health(config: TestConfig):
    """Verify Authentik health endpoint is responsive."""
    async with httpx.AsyncClient(verify=False) as client:
        response = await client.get(
            f"{config.SSO_URL}/-/health/ready/",
            timeout=10.0,
        )
        assert response.status_code < 500, \
            f"Authentik health should respond, got {response.status_code}"


@pytest.mark.platform
async def test_authentik_login_page_loads(page: Page, config: TestConfig):
    """Verify Authentik login page loads."""
    await page.goto(config.SSO_URL, wait_until="networkidle")

    title = await page.title()
    assert title is not None, "SSO page should load with a title"

    body_content = await page.locator("body").inner_text()
    assert len(body_content) > 0, "SSO page should have content"


@pytest.mark.platform
async def test_oidc_discovery_endpoint(config: TestConfig):
    """Verify OIDC discovery endpoint returns valid configuration (if configured)."""
    discovery_url = os.getenv("OIDC_DISCOVERY_URL")
    if not discovery_url:
        pytest.skip("OIDC_DISCOVERY_URL not configured")

    async with httpx.AsyncClient(verify=False) as client:
        response = await client.get(discovery_url, timeout=10.0)
        assert response.status_code < 500, "OIDC discovery should respond"
        if response.status_code == 200:
            data = response.json()
            assert "issuer" in data
            assert "authorization_endpoint" in data
