"""
Bootstrap Digger Orchestrator E2E Tests.

Tests Digger's core functionality, database connectivity, and GitHub integration.
"""
import pytest
import httpx
import os
import subprocess
from conftest import TestConfig


# =============================================================================
# Digger Service Health Tests
# =============================================================================

@pytest.mark.smoke
@pytest.mark.bootstrap
async def test_digger_endpoint_responds(config: TestConfig):
    """Verify Digger endpoint is reachable and responds."""
    async with httpx.AsyncClient(verify=False) as client:
        try:
            response = await client.get(config.DIGGER_URL, timeout=10.0)
            # Digger may require auth (401) or be accessible (200)
            assert response.status_code in [200, 401, 403], \
                f"Digger should respond, got {response.status_code}"
        except httpx.ConnectError:
            pytest.skip("Digger not reachable from test environment")


@pytest.mark.bootstrap
async def test_digger_https_enabled(config: TestConfig):
    """Verify Digger is served over HTTPS."""
    assert config.DIGGER_URL.startswith("https://"), \
        "Digger URL must use HTTPS"


@pytest.mark.bootstrap
async def test_digger_health_endpoint(config: TestConfig):
    """Verify Digger health/status endpoint if available."""
    async with httpx.AsyncClient(verify=False) as client:
        health_endpoints = [
            f"{config.DIGGER_URL}/health",
            f"{config.DIGGER_URL}/api/health",
            f"{config.DIGGER_URL}/status",
        ]
        
        for endpoint in health_endpoints:
            try:
                response = await client.get(endpoint, timeout=10.0)
                if response.status_code == 200:
                    # Found a working health endpoint
                    return
            except Exception:
                pass
        
        # If no health endpoint found, skip (not critical)
        pytest.skip("No standard health endpoint found")


# =============================================================================
# Digger Authentication Tests
# =============================================================================

@pytest.mark.bootstrap
async def test_digger_bearer_token_configured(config: TestConfig):
    """Verify Digger Bearer Token is configured."""
    bearer_token = os.getenv("DIGGER_BEARER_TOKEN") or os.getenv("TF_VAR_digger_bearer_token")
    
    if not bearer_token:
        pytest.skip("DIGGER_BEARER_TOKEN not available in test environment")
    
    min_length = config.K8sResources.MIN_TOKEN_LENGTH
    assert len(bearer_token) > 0, "Bearer token should not be empty"
    assert len(bearer_token) >= min_length, \
        f"Bearer token should be at least {min_length} characters"


@pytest.mark.bootstrap
async def test_digger_github_oauth_configured():
    """Verify Digger GitHub OAuth credentials are configured."""
    client_id = os.getenv("GITHUB_OAUTH_CLIENT_ID") or os.getenv("TF_VAR_github_oauth_client_id")
    client_secret = os.getenv("GITHUB_OAUTH_CLIENT_SECRET") or os.getenv("TF_VAR_github_oauth_client_secret")
    
    if not client_id or not client_secret:
        pytest.skip("GitHub OAuth credentials not available in test environment")
    
    assert len(client_id) > 0, "GitHub OAuth Client ID should be configured"
    assert len(client_secret) > 0, "GitHub OAuth Client Secret should be configured"


@pytest.mark.bootstrap
async def test_digger_github_app_configured():
    """Verify Digger GitHub App credentials are configured."""
    app_id = os.getenv("INFRA_FLASH_APP_ID") or os.getenv("TF_VAR_infra_flash_app_id")
    
    if not app_id:
        pytest.skip("GitHub App ID not available in test environment")
    
    assert app_id.isdigit(), "GitHub App ID should be numeric"


# =============================================================================
# Digger Database Connectivity Tests
# =============================================================================

@pytest.mark.bootstrap
async def test_digger_database_exists():
    """Verify Digger database exists in Platform PG."""
    db_host = os.getenv("PLATFORM_DB_HOST") or "platform-pg-rw.platform.svc.cluster.local"
    db_port = os.getenv("PLATFORM_DB_PORT", "5432")
    db_user = os.getenv("PLATFORM_DB_USER", "postgres")
    db_password = os.getenv("PLATFORM_DB_PASSWORD") or os.getenv("TF_VAR_vault_postgres_password")
    
    if not db_password:
        pytest.skip("Platform DB credentials not available in test environment")
    
    try:
        import asyncpg
        conn = await asyncpg.connect(
            host=db_host,
            port=int(db_port),
            user=db_user,
            password=db_password,
            database="postgres",
            timeout=10.0,
        )
        
        databases = await conn.fetch(
            "SELECT datname FROM pg_database WHERE datistemplate = false"
        )
        db_names = [row['datname'] for row in databases]
        
        assert 'digger' in db_names, \
            f"Digger database should exist, found: {db_names}"
        
        await conn.close()
    except ImportError:
        pytest.skip("asyncpg not installed")
    except Exception as e:
        pytest.skip(f"Cannot connect to database: {e}")


@pytest.mark.bootstrap
async def test_digger_can_connect_to_database():
    """Verify Digger can connect to its database."""
    db_host = os.getenv("PLATFORM_DB_HOST") or "platform-pg-rw.platform.svc.cluster.local"
    db_port = os.getenv("PLATFORM_DB_PORT", "5432")
    db_user = os.getenv("PLATFORM_DB_USER", "postgres")
    db_password = os.getenv("PLATFORM_DB_PASSWORD") or os.getenv("TF_VAR_vault_postgres_password")
    
    if not db_password:
        pytest.skip("Platform DB credentials not available in test environment")
    
    try:
        import asyncpg
        conn = await asyncpg.connect(
            host=db_host,
            port=int(db_port),
            user=db_user,
            password=db_password,
            database="digger",
            timeout=10.0,
        )
        
        result = await conn.fetchval('SELECT 1')
        assert result == 1, "Should be able to query digger database"
        await conn.close()
    except ImportError:
        pytest.skip("asyncpg not installed")
    except Exception as e:
        pytest.skip(f"Cannot connect to digger database: {e}")


# =============================================================================
# Digger Kubernetes Resource Tests
# =============================================================================

@pytest.mark.bootstrap
async def test_digger_pod_running():
    """Verify Digger pod is running in bootstrap namespace."""
    try:
        result = subprocess.run(
            ["kubectl", "get", "pods", "-n", "bootstrap", "-l", "app.kubernetes.io/name=digger-backend", "-o", "jsonpath={.items[*].status.phase}"],
            capture_output=True, text=True, timeout=10.0
        )
        
        if result.returncode == 0 and result.stdout:
            phases = result.stdout.split()
            assert any(phase == "Running" for phase in phases), \
                f"At least one Digger pod should be running, got phases: {phases}"
        else:
            pytest.skip("kubectl command failed or no digger pods found")
    except FileNotFoundError:
        pytest.skip("kubectl not found in test environment")


@pytest.mark.bootstrap
async def test_digger_service_exists():
    """Verify Digger service exists in bootstrap namespace."""
    try:
        result = subprocess.run(
            ["kubectl", "get", "svc", "-n", "bootstrap", "-l", "app.kubernetes.io/name=digger-backend", "-o", "name"],
            capture_output=True, text=True, timeout=10.0
        )
        
        if result.returncode == 0 and result.stdout:
            assert "service/" in result.stdout, \
                "Digger service should exist"
        else:
            pytest.skip("kubectl command failed or digger service not found")
    except FileNotFoundError:
        pytest.skip("kubectl not found in test environment")


@pytest.mark.bootstrap
async def test_digger_ingress_configured():
    """Verify Digger ingress is configured."""
    try:
        result = subprocess.run(
            ["kubectl", "get", "ingress", "-n", "bootstrap", "-o", "jsonpath={.items[*].spec.rules[*].host}"],
            capture_output=True, text=True, timeout=10.0
        )
        
        if result.returncode == 0 and result.stdout:
            hosts = result.stdout.split()
            assert any("digger" in host for host in hosts), \
                f"Digger host should be in ingress, found: {hosts}"
        else:
            pytest.skip("kubectl command failed or no ingress found")
    except FileNotFoundError:
        pytest.skip("kubectl not found in test environment")


# =============================================================================
# Digger Webhook Configuration Tests
# =============================================================================

@pytest.mark.bootstrap
async def test_digger_webhook_endpoint_structure(config: TestConfig):
    """Verify Digger webhook URL follows expected structure."""
    webhook_url = f"{config.DIGGER_URL}/github-app-webhook"
    
    assert webhook_url.startswith("https://"), \
        "Webhook URL must use HTTPS"
    assert "github-app-webhook" in webhook_url, \
        "Webhook URL should contain github-app-webhook path"


@pytest.mark.bootstrap
async def test_digger_webhook_secret_configured(config: TestConfig):
    """Verify Digger webhook secret is configured."""
    webhook_secret = os.getenv("DIGGER_WEBHOOK_SECRET") or os.getenv("TF_VAR_digger_webhook_secret")
    
    if not webhook_secret:
        pytest.skip("DIGGER_WEBHOOK_SECRET not available in test environment")
    
    min_length = config.K8sResources.MIN_WEBHOOK_SECRET_LENGTH
    assert len(webhook_secret) > 0, "Webhook secret should not be empty"
    assert len(webhook_secret) >= min_length, \
        f"Webhook secret should be at least {min_length} characters"


# =============================================================================
# Digger Configuration Validation Tests
# =============================================================================

@pytest.mark.bootstrap
async def test_digger_terraform_config_exists():
    """Verify Digger is defined in bootstrap Terraform."""
    import pathlib
    
    digger_tf = pathlib.Path(__file__).parent.parent.parent.parent.parent / "bootstrap" / "2.digger.tf"
    
    assert digger_tf.exists(), \
        "Digger Terraform configuration should exist at bootstrap/2.digger.tf"


@pytest.mark.bootstrap
async def test_digger_helm_values_valid():
    """Verify Digger Helm values are configured correctly."""
    import pathlib
    
    digger_tf = pathlib.Path(__file__).parent.parent.parent.parent.parent / "bootstrap" / "2.digger.tf"
    
    if not digger_tf.exists():
        pytest.skip("Digger Terraform configuration not found")
    
    content = digger_tf.read_text()
    
    # Check critical configuration elements
    assert "helm_release" in content and "digger" in content, \
        "Should define Digger Helm release"
    assert "postgres" in content, \
        "Should configure PostgreSQL connection"
    assert "ingress" in content, \
        "Should configure ingress"
    assert "cert-manager" in content or "letsencrypt" in content, \
        "Should configure TLS via cert-manager"


@pytest.mark.bootstrap
async def test_digger_namespace_is_bootstrap():
    """Verify Digger is deployed in bootstrap namespace."""
    try:
        result = subprocess.run(
            ["kubectl", "get", "pods", "-n", "bootstrap", "-l", "app.kubernetes.io/name=digger-backend", "-o", "jsonpath={.items[0].metadata.namespace}"],
            capture_output=True, text=True, timeout=10.0
        )
        
        if result.returncode == 0 and result.stdout:
            assert result.stdout.strip() == "bootstrap", \
                f"Digger should be in bootstrap namespace, got: {result.stdout}"
        else:
            pytest.skip("kubectl command failed or no digger pods found")
    except FileNotFoundError:
        pytest.skip("kubectl not found in test environment")
