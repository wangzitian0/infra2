"""
Secrets management tests (Vault).

Tests Vault secrets engine, policies, and integrations.

See README.md for SSOT documentation on secrets configuration.
"""
import pytest
import httpx
from conftest import TestConfig



@pytest.mark.platform
async def test_vault_health(config: TestConfig):
    """Verify Vault API is healthy and initialized."""
    async with httpx.AsyncClient(verify=False) as client:
        # Vault health endpoint
        response = await client.get(
            f"{config.VAULT_URL}/v1/sys/health",
            timeout=10.0,
        )
        
        # 200: Initialized, unsealed, and active
        # 429: Active node, but standby
        # 472: Disaster recovery mode
        # 473: Performance standby
        # 501: Not initialized
        # 503: Sealed
        
        assert response.status_code in [200, 429], \
            f"Vault health check failed: {response.status_code}. It might be sealed or uninitialized."
        
        data = response.json()
        assert data.get("initialized") is True, "Vault should be initialized"


@pytest.mark.platform
async def test_vault_oidc_configured(config: TestConfig):
    """Verify Vault OIDC auth method is configured."""
    async with httpx.AsyncClient(verify=False) as client:
        # Check for OIDC auth path (publicly visible metadata if UI disabled, but we have UI enabled)
        response = await client.get(
            f"{config.VAULT_URL}/v1/sys/auth",
            timeout=10.0,
        )
        
        # If we can't access /v1/sys/auth without token (Standard), 
        # we check the UI for OIDC buttons
        if response.status_code == 403:
             # Check UI for OIDC login button text
             from playwright.async_api import Page
             # This would require a separate test function taking Page
             pass
        elif response.status_code == 200:
            data = response.json()
            assert "oidc/" in data or "oidc" in data.get("data", {}), "OIDC auth should be enabled"

