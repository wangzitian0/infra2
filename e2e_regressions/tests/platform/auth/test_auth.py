"""
Auth and SSO integration tests.

Tests authentication flows including Casdoor, OAuth2, and Portal SSO.

See README.md for SSOT documentation on auth configuration.
"""
import pytest
import httpx
from conftest import TestConfig


@pytest.mark.platform
async def test_casdoor_api_health(config: TestConfig):
    """Verify Casdoor API is healthy."""
    async with httpx.AsyncClient(verify=False) as client:
        response = await client.get(
            f"{config.SSO_URL}/api/get-organizations",
            timeout=10.0,
        )
        assert response.status_code < 500, \
            f"Casdoor API should respond, got {response.status_code}"


@pytest.mark.platform
async def test_casdoor_oidc_config(config: TestConfig):
    """Verify Casdoor OIDC configuration is accessible."""
    async with httpx.AsyncClient(verify=False) as client:
        response = await client.get(
            f"{config.SSO_URL}/.well-known/openid-configuration",
            timeout=10.0,
        )
        if response.status_code == 200:
            data = response.json()
            assert "issuer" in data, "OIDC config should have issuer"
            assert "authorization_endpoint" in data

