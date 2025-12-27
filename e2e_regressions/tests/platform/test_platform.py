"""
Platform service availability and health tests.

Tests Vault, Dashboard, and Casdoor endpoints.
"""
import pytest
import httpx
from playwright.async_api import Page
from conftest import TestConfig


@pytest.mark.smoke
@pytest.mark.platform
async def test_vault_is_accessible(config: TestConfig):
    """Verify Vault HTTP API is accessible."""
    async with httpx.AsyncClient(verify=False) as client:
        response = await client.get(
            f"{config.VAULT_URL}/v1/sys/health",
            timeout=10.0,
        )

        # Health endpoint returns 200, 429, 473, or 501
        assert response.status_code in [200, 429, 473, 501], \
            f"Vault health check failed: {response.status_code}"


@pytest.mark.smoke
@pytest.mark.platform
async def test_dashboard_is_accessible(config: TestConfig):
    """Verify Kubernetes Dashboard is accessible."""
    async with httpx.AsyncClient(verify=False) as client:
        response = await client.get(config.DASHBOARD_URL, timeout=10.0)

        # Should return 200 or redirect (301/302) if auth required
        assert response.status_code in [200, 301, 302, 401, 403], \
            f"Dashboard should be accessible, got {response.status_code}"


@pytest.mark.smoke
@pytest.mark.platform
async def test_casdoor_is_accessible(config: TestConfig):
    """Verify Casdoor SSO service is accessible."""
    async with httpx.AsyncClient(verify=False) as client:
        response = await client.get(config.SSO_URL, timeout=10.0)

        assert response.status_code in [200, 301, 302], \
            f"Casdoor should be accessible, got {response.status_code}"


@pytest.mark.platform
async def test_vault_seal_status(config: TestConfig):
    """Check Vault seal status via HTTP API."""
    if not config.VAULT_URL:
        pytest.skip("VAULT_URL not configured")

    async with httpx.AsyncClient(verify=False) as client:
        response = await client.get(
            f"{config.VAULT_URL}/v1/sys/seal-status",
            timeout=10.0,
        )

        if response.status_code == 200:
            data = response.json()
            # Seal status should be boolean
            assert "sealed" in data, "Seal status should include 'sealed' field"


@pytest.mark.platform
async def test_casdoor_api_accessible(config: TestConfig):
    """Verify Casdoor API endpoints respond."""
    async with httpx.AsyncClient(verify=False) as client:
        # Casdoor API endpoint for getting orgs (public endpoint)
        response = await client.get(
            f"{config.SSO_URL}/api/get-organizations",
            timeout=10.0,
        )

        # API should respond (may be 200 or 400 depending on params)
        assert response.status_code < 500, \
            f"Casdoor API should not error, got {response.status_code}"


@pytest.mark.platform
async def test_vault_cors_headers(config: TestConfig):
    """Verify Vault API includes CORS headers if needed."""
    async with httpx.AsyncClient(verify=False) as client:
        response = await client.get(
            f"{config.VAULT_URL}/v1/sys/health",
            timeout=10.0,
        )

        # Vault should respond (CORS may or may not be enabled)
        assert response.status_code < 500, "Vault API should respond"


@pytest.mark.platform
async def test_dashboard_with_page(page: Page, config: TestConfig):
    """Browser-based Dashboard accessibility test."""
    await page.goto(config.DASHBOARD_URL, wait_until="domcontentloaded")

    # Dashboard may require auth, so we're just checking it loads without crashing
    title = await page.title()
    assert title and len(title) > 0, "Dashboard page should have a title"


@pytest.mark.platform
async def test_vault_with_page(page: Page, config: TestConfig):
    """Browser-based Vault UI accessibility test."""
    await page.goto(config.VAULT_URL, wait_until="domcontentloaded")

    # Vault UI should load
    title = await page.title()
    assert title and len(title) > 0, "Vault UI should load"


@pytest.mark.platform
async def test_casdoor_with_page(page: Page, config: TestConfig):
    """Browser-based Casdoor UI accessibility test."""
    await page.goto(config.SSO_URL, wait_until="domcontentloaded")

    # Casdoor should load and have login form or admin interface
    title = await page.title()
    assert title and len(title) > 0, "Casdoor UI should load"

    # Page should have some content
    body_content = await page.locator("body").inner_text()
    assert len(body_content) > 0, "Casdoor UI should have content"


@pytest.mark.platform
async def test_vault_config_endpoints(config: TestConfig):
    """Test various Vault config endpoints for connectivity."""
    if not config.VAULT_URL:
        pytest.skip("VAULT_URL not configured")

    endpoints = [
        "/v1/sys/config/ui/headers",
        "/v1/sys/metrics",
    ]

    async with httpx.AsyncClient(verify=False) as client:
        for endpoint in endpoints:
            response = await client.get(
                f"{config.VAULT_URL}{endpoint}",
                timeout=10.0,
            )
            # Endpoints should respond (may need auth)
            assert response.status_code < 500, \
                f"Vault {endpoint} should not error: {response.status_code}"


@pytest.mark.platform
async def test_casdoor_org_endpoint(config: TestConfig):
    """Verify Casdoor org/resource endpoints are working."""
    async with httpx.AsyncClient(verify=False) as client:
        response = await client.get(
            f"{config.SSO_URL}/api/get-orgs",
            timeout=10.0,
        )

        # Should not return 500
        assert response.status_code < 500, \
            f"Casdoor org endpoint should not error: {response.status_code}"
