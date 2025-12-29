"""
Platform service availability and health tests.

Tests Vault and Authentik endpoints.
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
        assert response.status_code in [200, 429, 472, 473, 501], \
            f"Vault health check failed: {response.status_code}"


@pytest.mark.smoke
@pytest.mark.platform
async def test_sso_is_accessible(config: TestConfig):
    """Verify Authentik SSO service is accessible."""
    async with httpx.AsyncClient(verify=False) as client:
        response = await client.get(config.SSO_URL, timeout=10.0)
        assert response.status_code < 500, \
            f"SSO should be accessible, got {response.status_code}"


@pytest.mark.platform
async def test_vault_seal_status(config: TestConfig):
    """Check Vault seal status via HTTP API."""
    async with httpx.AsyncClient(verify=False) as client:
        response = await client.get(
            f"{config.VAULT_URL}/v1/sys/seal-status",
            timeout=10.0,
        )
        if response.status_code == 200:
            data = response.json()
            assert "sealed" in data, "Seal status should include 'sealed' field"


@pytest.mark.platform
async def test_vault_with_page(page: Page, config: TestConfig):
    """Browser-based Vault UI accessibility test."""
    await page.goto(config.VAULT_URL, wait_until="domcontentloaded")
    title = await page.title()
    assert title and len(title) > 0, "Vault UI should load"


@pytest.mark.platform
async def test_sso_with_page(page: Page, config: TestConfig):
    """Browser-based SSO UI accessibility test."""
    await page.goto(config.SSO_URL, wait_until="domcontentloaded")

    title = await page.title()
    assert title and len(title) > 0, "SSO UI should load"

    body_content = await page.locator("body").inner_text()
    assert len(body_content) > 0, "SSO UI should have content"
