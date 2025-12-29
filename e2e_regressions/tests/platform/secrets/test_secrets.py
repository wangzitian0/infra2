"""
Secrets management tests (Vault).

Tests Vault secrets engine, policies, and integrations.

See README.md for SSOT documentation on secrets configuration.
"""
import pytest
from conftest import TestConfig



@pytest.mark.platform
async def test_vault_health(config: TestConfig, http_client):
    """Verify Vault API is healthy and initialized."""
    # Vault health endpoint
    response = await http_client.get(f"{config.VAULT_URL}/v1/sys/health")

    # 200: Initialized, unsealed, and active
    # 429: Active node, but standby
    assert response.status_code in [200, 429], \
        f"Vault health check failed: {response.status_code}. It might be sealed or uninitialized."

    data = response.json()
    assert data.get("initialized") is True, "Vault should be initialized"


@pytest.mark.platform
async def test_vault_oidc_configured(config: TestConfig, http_client):
    """Verify Vault OIDC auth method is configured."""
    # Check for OIDC auth path (publicly visible metadata if UI disabled, but we have UI enabled)
    response = await http_client.get(f"{config.VAULT_URL}/v1/sys/auth")

    # If we can't access /v1/sys/auth without token (Standard), skip.
    if response.status_code == 403:
        pytest.skip("Vault /v1/sys/auth requires auth; cannot verify OIDC without token")
    elif response.status_code == 200:
        data = response.json()
        assert "oidc/" in data or "oidc" in data.get("data", {}), "OIDC auth should be enabled"
    else:
        pytest.fail(f"Unexpected status from /v1/sys/auth: {response.status_code}")
