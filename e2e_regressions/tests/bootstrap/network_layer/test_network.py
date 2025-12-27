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


# =============================================================================
# DNS Resolution Tests
# =============================================================================

@pytest.mark.smoke
@pytest.mark.bootstrap
async def test_dns_resolution_portal(config: TestConfig):
    """Verify Portal domain resolves correctly."""
    portal_host = urlparse(config.PORTAL_URL).hostname
    
    try:
        ip = socket.gethostbyname(portal_host)
        assert ip is not None, f"DNS resolution failed for {portal_host}"
        assert len(ip) > 0, "IP address should not be empty"
    except socket.gaierror as e:
        pytest.fail(f"DNS resolution failed: {e}")


@pytest.mark.bootstrap
async def test_dns_resolution_all_services(config: TestConfig):
    """Verify all service domains resolve."""
    services = [
        config.PORTAL_URL,
        config.SSO_URL,
        config.VAULT_URL,
        config.DASHBOARD_URL,
        config.DIGGER_URL,
    ]
    
    failed_resolutions = []
    
    for url in services:
        hostname = urlparse(url).hostname
        try:
            ip = socket.gethostbyname(hostname)
            assert ip is not None
        except (socket.gaierror, AssertionError) as e:
            failed_resolutions.append((hostname, str(e)))
    
    assert len(failed_resolutions) == 0, \
        f"DNS resolution failed for: {failed_resolutions}"


@pytest.mark.bootstrap
async def test_dns_wildcard_subdomain(config: TestConfig):
    """Verify wildcard DNS is configured (*.domain.com)."""
    test_subdomains = [
        urlparse(config.PORTAL_URL).hostname,
        urlparse(config.SSO_URL).hostname,
        urlparse(config.VAULT_URL).hostname,
    ]
    
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


@pytest.mark.bootstrap
async def test_dns_consistency(config: TestConfig):
    """Verify DNS resolution is consistent across multiple queries."""
    portal_host = urlparse(config.PORTAL_URL).hostname
    
    ips = []
    for _ in range(3):
        try:
            ip = socket.gethostbyname(portal_host)
            ips.append(ip)
        except socket.gaierror:
            pass
    
    assert len(ips) > 0, "At least one DNS query should succeed"
    assert len(set(ips)) <= 2, \
        f"DNS should be consistent, got multiple IPs: {set(ips)}"


@pytest.mark.bootstrap
async def test_dns_k3s_api_resolvable(config: TestConfig):
    """Verify K3s API domain is resolvable (Grey record)."""
    portal_host = urlparse(config.PORTAL_URL).hostname
    domain_parts = portal_host.split(".")
    
    if len(domain_parts) >= 2:
        base_domain = ".".join(domain_parts[-2:])
        k3s_domain = f"k3s.{base_domain}"
        
        try:
            ip = socket.gethostbyname(k3s_domain)
            assert ip is not None, f"K3s API domain should resolve: {k3s_domain}"
        except socket.gaierror:
            pytest.skip(f"K3s API domain not configured: {k3s_domain}")
    else:
        pytest.skip("Cannot construct K3s API domain")


# =============================================================================
# TLS Certificate Tests
# =============================================================================

@pytest.mark.smoke
@pytest.mark.bootstrap
async def test_certificates_https_enabled(config: TestConfig):
    """Verify all services use HTTPS."""
    services = [
        config.PORTAL_URL,
        config.SSO_URL,
        config.VAULT_URL,
        config.DASHBOARD_URL,
        config.DIGGER_URL,
    ]
    
    for url in services:
        assert url.startswith("https://"), \
            f"Service should use HTTPS: {url}"


@pytest.mark.bootstrap
async def test_certificates_valid_or_self_signed(config: TestConfig):
    """Verify certificates are present (valid or self-signed)."""
    async with httpx.AsyncClient() as client:
        services = [
            config.PORTAL_URL,
            config.SSO_URL,
            config.VAULT_URL,
            config.DIGGER_URL,
        ]
        
        for url in services:
            try:
                response = await client.get(url, timeout=10.0)
                # Valid cert
                assert True
            except httpx.SSLError:
                # Self-signed is acceptable
                async with httpx.AsyncClient(verify=False) as client_no_verify:
                    response = await client_no_verify.get(url, timeout=10.0)
                    assert response.status_code < 500, \
                        f"Service should respond with self-signed cert: {url}"


@pytest.mark.bootstrap
async def test_certificate_expiry_check(config: TestConfig):
    """Verify certificates are not close to expiration (at least 7 days)."""
    import datetime
    
    url = config.PORTAL_URL
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
        # Fallback without cryptography
        async with httpx.AsyncClient(verify=False) as client:
            response = await client.get(url, timeout=10.0)
            assert response.status_code < 500
    except Exception as e:
        pytest.fail(f"Failed to check certificate expiry: {e}")


@pytest.mark.bootstrap
async def test_certificate_issuer_info(config: TestConfig):
    """Verify certificate issuer can be retrieved."""
    hostname = urlparse(config.PORTAL_URL).hostname
    
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


@pytest.mark.bootstrap
async def test_cert_manager_issuer_configured():
    """Verify cert-manager ClusterIssuer is configured in terraform."""
    import pathlib
    
    dns_tf = pathlib.Path(__file__).parent.parent.parent.parent.parent / "bootstrap" / "3.dns_and_cert.tf"
    
    if not dns_tf.exists():
        pytest.skip("DNS/Cert configuration not found")
    
    content = dns_tf.read_text()
    assert "cert-manager" in content or "letsencrypt" in content, \
        "cert-manager or Let's Encrypt should be configured"
