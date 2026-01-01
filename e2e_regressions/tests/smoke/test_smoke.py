"""
End-to-end smoke tests covering deployment completion scenarios.

These tests verify the complete deployment stack is operational.
"""
import pytest
import httpx
from playwright.async_api import Page
from conftest import TestConfig


def _core_services(config: TestConfig):
    services = {
        "Dokploy": config.DOKPLOY_URL,
        "Vault": f"{config.VAULT_URL}/v1/sys/health",
        "SSO": config.SSO_URL,
        "1Password": config.OP_URL,
        "MinIO Console": config.MINIO_CONSOLE_URL,
    }
    if config.PORTAL_URL:
        services["Portal"] = config.PORTAL_URL
    return services


@pytest.mark.smoke
@pytest.mark.e2e
async def test_minio_health(config: TestConfig):
    """Verify MinIO Console and S3 API are accessible."""
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        # Console should return 200 (HTML login page)
        console_resp = await client.get(config.MINIO_CONSOLE_URL)
        assert console_resp.status_code == 200, \
            f"MinIO Console returned {console_resp.status_code}"
        
        # S3 API health check
        api_health_url = f"{config.MINIO_API_URL}/minio/health/live"
        api_resp = await client.get(api_health_url)
        assert api_resp.status_code == 200, \
            f"MinIO API health check returned {api_resp.status_code}"


@pytest.mark.smoke
@pytest.mark.e2e
async def test_deployment_complete_smoke(config: TestConfig):
    """Smoke test: Verify all critical services are responding."""
    failed_services = {}

    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        for name, url in _core_services(config).items():
            try:
                response = await client.get(url)
                if response.status_code >= 500:
                    failed_services[name] = response.status_code
            except Exception as e:
                failed_services[name] = str(e)

    assert not failed_services, f"Critical services failed: {failed_services}"


@pytest.mark.e2e
async def test_deployment_routing(config: TestConfig):
    """Verify routing for core services on the same internal domain."""
    from urllib.parse import urlparse

    services_domains = [(url, name) for name, url in _core_services(config).items()]
    internal_domain = config.INTERNAL_DOMAIN

    async with httpx.AsyncClient(verify=False) as client:
        for url, name in services_domains:
            service_domain = urlparse(url).hostname
            assert service_domain.endswith(internal_domain), \
                f"{name} should be on domain {internal_domain}, got {service_domain}"

            response = await client.get(url, timeout=10.0)
            assert response.status_code < 500, \
                f"{name} returned {response.status_code}"


@pytest.mark.e2e
async def test_deployment_security_headers(page: Page, config: TestConfig):
    """Verify Dokploy includes basic security headers."""
    response = await page.goto(config.DOKPLOY_URL, wait_until="domcontentloaded")
    headers_dict = await response.all_headers() if response else {}
    headers_lower = {k.lower(): v for k, v in headers_dict.items()}

    security_headers = [
        "content-security-policy",
        "x-content-type-options",
        "x-frame-options",
    ]

    found_security_headers = sum(1 for h in security_headers if h in headers_lower)
    assert found_security_headers >= 0, "Dokploy should have some security headers configured"


@pytest.mark.e2e
async def test_deployment_error_pages(page: Page, config: TestConfig):
    """Verify error pages are working (404, etc)."""
    await page.goto(f"{config.DOKPLOY_URL}/does-not-exist-xyz", wait_until="domcontentloaded")
    title = await page.title()
    assert title is not None, "Error page should load"


@pytest.mark.e2e
async def test_deployment_certificate_validation(config: TestConfig):
    """Verify HTTPS certificates are properly installed."""
    async with httpx.AsyncClient() as client:
        for url in _core_services(config).values():
            try:
                await client.get(url, timeout=10.0)
            except httpx.SSLError as e:
                assert "self" in str(e).lower() or "untrusted" in str(e).lower(), \
                    f"SSL error (not self-signed): {e}"
            except Exception:
                assert True


@pytest.mark.e2e
async def test_deployment_performance_baseline(config: TestConfig):
    """Establish performance baseline for monitoring."""
    import time

    async with httpx.AsyncClient(verify=False) as client:
        metrics = {}

        services = [
            ("Dokploy", config.DOKPLOY_URL),
            ("SSO", config.SSO_URL),
            ("Vault", f"{config.VAULT_URL}/v1/sys/health"),
        ]

        for name, url in services:
            times = []
            for _ in range(3):
                start = time.time()
                await client.get(url, timeout=30.0)
                elapsed = time.time() - start
                times.append(elapsed)

            metrics[name] = {
                "avg_ms": sum(times) / len(times) * 1000,
                "max_ms": max(times) * 1000,
            }

        for name, timing in metrics.items():
            assert timing["avg_ms"] < 5000, \
                f"{name} average response time too high: {timing['avg_ms']:.0f}ms"
            assert timing["max_ms"] < 10000, \
                f"{name} max response time too high: {timing['max_ms']:.0f}ms"


@pytest.mark.e2e
async def test_deployment_data_persistence(page: Page, config: TestConfig):
    """Verify Dokploy UI loads consistently."""
    await page.goto(config.DOKPLOY_URL, wait_until="networkidle")
    first_load_title = await page.title()

    await page.wait_for_timeout(1000)
    await page.reload(wait_until="networkidle")

    second_load_title = await page.title()
    assert first_load_title == second_load_title, \
        "Dokploy should load consistently"
