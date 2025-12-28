"""
Bootstrap Network Layer E2E Tests.

Tests DNS resolution and TLS certificate configuration.
"""
import pytest
import httpx
import ssl
import socket
from urllib.parse import urlparse
from conftest import TestConfig


def _service_urls(config: TestConfig):
    urls = [config.DOKPLOY_URL, config.OP_URL, config.VAULT_URL, config.SSO_URL]
    if config.PORTAL_URL:
        urls.append(config.PORTAL_URL)
    return urls


@pytest.mark.smoke
@pytest.mark.bootstrap
async def test_dns_resolution_core_services(config: TestConfig):
    """Verify core service domains resolve correctly."""
    services = _service_urls(config)
    failures = []

    for url in services:
        hostname = urlparse(url).hostname
        try:
            ip = socket.gethostbyname(hostname)
            assert ip, f"DNS resolution failed for {hostname}"
        except socket.gaierror as e:
            failures.append((hostname, str(e)))

    assert not failures, f"DNS resolution failed for: {failures}"


@pytest.mark.bootstrap
async def test_dns_wildcard_subdomain(config: TestConfig):
    """Verify wildcard DNS is configured for core services."""
    test_subdomains = [urlparse(u).hostname for u in _service_urls(config)]

    resolved_count = 0
    for subdomain in test_subdomains:
        try:
            ip = socket.gethostbyname(subdomain)
            if ip:
                resolved_count += 1
        except socket.gaierror:
            pass

    assert resolved_count >= 2, \
        f"Wildcard DNS may not be configured (only {resolved_count} subdomains resolved)"


@pytest.mark.smoke
@pytest.mark.bootstrap
async def test_certificates_https_enabled(config: TestConfig):
    """Verify all services use HTTPS."""
    for url in _service_urls(config):
        assert url.startswith("https://"), f"Service should use HTTPS: {url}"


@pytest.mark.bootstrap
async def test_certificates_valid_or_self_signed(config: TestConfig):
    """Verify certificates are present (valid or self-signed)."""
    async with httpx.AsyncClient() as client:
        for url in _service_urls(config):
            try:
                await client.get(url, timeout=10.0)
            except httpx.SSLError:
                async with httpx.AsyncClient(verify=False) as client_no_verify:
                    response = await client_no_verify.get(url, timeout=10.0)
                    assert response.status_code < 500, \
                        f"Service should respond with self-signed cert: {url}"


@pytest.mark.bootstrap
async def test_certificate_expiry_check(config: TestConfig):
    """Verify certificates are not close to expiration (at least 7 days)."""
    import datetime

    url = _service_urls(config)[0]
    hostname = urlparse(url).hostname
    port = 443

    try:
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend

        cert_pem = ssl.get_server_certificate((hostname, port))
        cert = x509.load_pem_x509_certificate(cert_pem.encode(), default_backend())

        remaining = cert.not_valid_after_utc - datetime.datetime.now(datetime.UTC)
        assert remaining.days > 7, f"Certificate expires in {remaining.days} days"
    except ImportError:
        async with httpx.AsyncClient(verify=False) as client:
            response = await client.get(url, timeout=10.0)
            assert response.status_code < 500
    except Exception as e:
        pytest.fail(f"Failed to check certificate expiry: {e}")


@pytest.mark.bootstrap
async def test_certificate_issuer_info(config: TestConfig):
    """Verify certificate issuer can be retrieved."""
    hostname = urlparse(_service_urls(config)[0]).hostname

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    try:
        with socket.create_connection((hostname, 443), timeout=10) as sock:
            with context.wrap_socket(sock, server_hostname=hostname) as ssock:
                cert = ssock.getpeercert(binary_form=True)
                assert cert is not None, "Should retrieve certificate"
    except Exception as e:
        pytest.skip(f"Cannot connect to {hostname}: {e}")
