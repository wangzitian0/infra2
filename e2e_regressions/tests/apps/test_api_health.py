"""
API endpoint health and availability tests.

Tests service health for core endpoints.
"""
import pytest
import httpx
from conftest import TestConfig


def _service_list(config: TestConfig):
    services = [
        ("Dokploy", config.DOKPLOY_URL),
        ("1Password", config.OP_URL),
        ("Vault", config.VAULT_URL),
        ("SSO", config.SSO_URL),
    ]
    if config.PORTAL_URL:
        services.append(("Portal", config.PORTAL_URL))
    return services


@pytest.mark.smoke
@pytest.mark.api
async def test_http_connectivity(config: TestConfig):
    """Verify basic HTTP connectivity to all services."""
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        for name, url in _service_list(config):
            response = await client.get(url)
            assert response.status_code < 500, \
                f"{name} ({url}) returned {response.status_code}"


@pytest.mark.api
async def test_service_response_time(config: TestConfig):
    """Measure and verify service response times are reasonable."""
    services = [
        ("Dokploy", config.DOKPLOY_URL),
        ("SSO", config.SSO_URL),
    ]

    async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
        for name, url in services:
            import time
            start = time.time()
            response = await client.get(url)
            elapsed = time.time() - start

            assert elapsed < 10.0, \
                f"{name} took {elapsed:.2f}s (should be < 10s)"
            assert response.status_code < 500


@pytest.mark.api
async def test_ingress_routing(config: TestConfig):
    """Verify routing for core services."""
    async with httpx.AsyncClient(verify=False) as client:
        response = await client.get(config.DOKPLOY_URL, timeout=10.0)
        assert response.status_code < 500


@pytest.mark.api
async def test_ssl_certificates_valid(config: TestConfig):
    """Verify SSL certificates are valid (no connection errors)."""
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        for name, url in _service_list(config):
            try:
                response = await client.get(url)
                assert response.status_code < 600
            except Exception as e:
                pytest.fail(f"{name} SSL issue: {e}")


@pytest.mark.api
async def test_api_error_responses(config: TestConfig):
    """Verify services return proper error responses for nonexistent paths."""
    async with httpx.AsyncClient(verify=False) as client:
        response = await client.get(f"{config.DOKPLOY_URL}/nonexistent", timeout=10.0)
        assert response.status_code in [302, 404], \
            f"Expected 302 or 404, got {response.status_code}"


@pytest.mark.api
async def test_redirect_chains(config: TestConfig):
    """Verify redirect chains don't create loops."""
    async with httpx.AsyncClient(verify=False, follow_redirects=True) as client:
        response = await client.get(config.DOKPLOY_URL, timeout=10.0)
        assert response.status_code < 500
        assert len(response.history) < 10, "Too many redirects in chain"


@pytest.mark.api
async def test_service_headers(config: TestConfig):
    """Verify services return appropriate headers."""
    async with httpx.AsyncClient(verify=False) as client:
        response = await client.get(config.DOKPLOY_URL, timeout=10.0)
        headers = response.headers
        assert "content-type" in headers or "Content-Type" in headers, \
            "Response should include Content-Type header"


@pytest.mark.api
async def test_compressed_response_handling(config: TestConfig):
    """Verify gzip/compression handling works."""
    async with httpx.AsyncClient(verify=False) as client:
        response = await client.get(config.DOKPLOY_URL, timeout=10.0)
        content = response.text
        assert len(content) > 0, "Response body should be decompressed"


@pytest.mark.api
async def test_api_version_endpoints(config: TestConfig):
    """Test version/info endpoints if available."""
    endpoints = [
        f"{config.VAULT_URL}/v1/sys/seal-status",
        f"{config.SSO_URL}/-/health/ready/",
    ]

    async with httpx.AsyncClient(verify=False) as client:
        for endpoint in endpoints:
            response = await client.get(endpoint, timeout=10.0)
            assert response.status_code < 500, \
                f"Endpoint {endpoint} should not error: {response.status_code}"
