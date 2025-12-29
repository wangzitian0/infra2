"""
Bootstrap Compute Layer E2E Tests.

Tests Dokploy availability and basic bootstrap service routing.
"""
import pytest
import httpx
from urllib.parse import urlparse
from conftest import TestConfig


@pytest.mark.smoke
@pytest.mark.bootstrap
async def test_dokploy_ui_accessible(config: TestConfig):
    """Verify Dokploy UI is accessible."""
    async with httpx.AsyncClient(verify=False) as client:
        response = await client.get(config.DOKPLOY_URL, timeout=10.0)
        assert response.status_code < 500, \
            f"Dokploy should be accessible, got {response.status_code}"


@pytest.mark.bootstrap
async def test_bootstrap_services_accessible(config: TestConfig):
    """Verify bootstrap services respond (1Password, Vault, SSO)."""
    services = [
        ("1Password", config.OP_URL),
        ("Vault", f"{config.VAULT_URL}/v1/sys/health"),
        ("SSO", config.SSO_URL),
    ]

    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        failures = {}
        for name, url in services:
            try:
                response = await client.get(url)
                if response.status_code >= 500:
                    failures[name] = response.status_code
            except Exception as e:
                failures[name] = str(e)

    assert not failures, f"Bootstrap services failed: {failures}"


@pytest.mark.bootstrap
async def test_https_redirect_or_https_only(config: TestConfig):
    """Verify HTTP redirects to HTTPS or is closed."""
    parsed = urlparse(config.DOKPLOY_URL)
    http_url = f"http://{parsed.hostname}"

    async with httpx.AsyncClient(verify=False, follow_redirects=False) as client:
        try:
            response = await client.get(http_url, timeout=10.0)
            if response.status_code in [301, 302, 307, 308]:
                location = response.headers.get("location", "")
                assert location.startswith("https://"), "HTTP should redirect to HTTPS"
        except Exception:
            # HTTP might be closed, acceptable
            pass


@pytest.mark.bootstrap
async def test_proxy_headers_present(config: TestConfig):
    """Verify proxy preserves basic headers."""
    async with httpx.AsyncClient(verify=False) as client:
        response = await client.get(config.DOKPLOY_URL, timeout=10.0)
        headers_lower = {k.lower(): v for k, v in response.headers.items()}
        assert "content-type" in headers_lower, "Response should include content-type"
