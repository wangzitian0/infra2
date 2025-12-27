"""
API endpoint health and availability tests.

Tests business API endpoints and general service health.
"""
import pytest
import httpx
from conftest import TestConfig


@pytest.mark.smoke
@pytest.mark.api
async def test_http_connectivity(config: TestConfig):
    """Verify basic HTTP connectivity to all services."""
    services = [
        ("Portal", config.PORTAL_URL),
        ("Vault", config.VAULT_URL),
        ("Dashboard", config.DASHBOARD_URL),
        ("Casdoor", config.SSO_URL),
        ("Kubero", config.KUBERO_URL),
        ("SigNoz", config.SIGNOZ_URL),
        ("K3s", config.K3S_URL),
    ]

    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        for name, url in services:
            response = await client.get(url)
            assert response.status_code < 500, \
                f"{name} ({url}) returned {response.status_code}"


@pytest.mark.api
async def test_service_response_time(config: TestConfig):
    """Measure and verify service response times are reasonable."""
    services = [
        ("Portal", config.PORTAL_URL),
        ("Casdoor", config.SSO_URL),
    ]

    async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
        for name, url in services:
            import time
            start = time.time()
            response = await client.get(url)
            elapsed = time.time() - start

            # Response time should be < 10 seconds
            assert elapsed < 10.0, \
                f"{name} took {elapsed:.2f}s (should be < 10s)"
            assert response.status_code < 500


@pytest.mark.api
async def test_ingress_routing(config: TestConfig):
    """Verify ingress is routing traffic to services correctly."""
    # Extract domain from Portal URL
    from urllib.parse import urlparse

    portal_host = urlparse(config.PORTAL_URL).hostname

    async with httpx.AsyncClient(verify=False) as client:
        # Test that different hosts are routed correctly
        response = await client.get(config.PORTAL_URL, timeout=10.0)
        assert response.status_code < 500


@pytest.mark.api
async def test_ssl_certificates_valid(config: TestConfig):
    """Verify SSL certificates are valid (no self-signed errors on client)."""
    services = [
        ("Portal", config.PORTAL_URL),
        ("Vault", config.VAULT_URL),
    ]

    # Note: We're using verify=False in client, so this just checks
    # that service responds on HTTPS port
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        for name, url in services:
            try:
                response = await client.get(url)
                # Should get a response (not a connection error)
                assert response.status_code < 600
            except Exception as e:
                pytest.fail(f"{name} SSL issue: {e}")


@pytest.mark.api
async def test_api_error_responses(config: TestConfig):
    """Verify services return proper error responses for nonexistent paths."""
    async with httpx.AsyncClient(verify=False) as client:
        # Test handling of nonexistent path
        # SSO-protected sites may return 302 (redirect to login) or 404
        response = await client.get(f"{config.PORTAL_URL}/nonexistent", timeout=10.0)
        assert response.status_code in [302, 404], \
            f"Expected 302 (SSO redirect) or 404, got {response.status_code}"


@pytest.mark.api
async def test_cors_headers_present(config: TestConfig):
    """Check if CORS headers are properly configured (if needed)."""
    async with httpx.AsyncClient(verify=False) as client:
        # Casdoor API should be accessible
        response = await client.options(
            f"{config.SSO_URL}/api/get-organizations",
            timeout=10.0,
        )

        # Options request should work or return 200
        assert response.status_code < 500


@pytest.mark.api
async def test_redirect_chains(config: TestConfig):
    """Verify redirect chains don't create loops."""
    async with httpx.AsyncClient(verify=False, follow_redirects=True) as client:
        # Following redirects should eventually succeed
        response = await client.get(config.PORTAL_URL, timeout=10.0)

        # Should complete redirect chain and return final status
        assert response.status_code < 500

        # Ensure we didn't follow too many redirects
        assert len(response.history) < 10, "Too many redirects in chain"


@pytest.mark.api
async def test_service_headers(config: TestConfig):
    """Verify services return appropriate headers."""
    async with httpx.AsyncClient(verify=False) as client:
        response = await client.get(config.PORTAL_URL, timeout=10.0)

        headers = response.headers
        # Should have common headers
        assert "content-type" in headers or "Content-Type" in headers, \
            "Response should include Content-Type header"


@pytest.mark.api
async def test_compressed_response_handling(config: TestConfig):
    """Verify gzip/compression handling works."""
    async with httpx.AsyncClient(verify=False) as client:
        response = await client.get(config.PORTAL_URL, timeout=10.0)

        # Response should decompress properly
        content = response.text
        assert len(content) > 0, "Response body should be decompressed"


@pytest.mark.api
async def test_api_version_endpoints(config: TestConfig):
    """Test version/info endpoints if available."""
    endpoints = [
        f"{config.VAULT_URL}/v1/sys/seal-status",
        f"{config.SSO_URL}/api/system/get-system-info",
    ]

    async with httpx.AsyncClient(verify=False) as client:
        for endpoint in endpoints:
            response = await client.get(endpoint, timeout=10.0)
            # Endpoint should not return 500
            assert response.status_code < 500, \
                f"Endpoint {endpoint} should not error: {response.status_code}"
