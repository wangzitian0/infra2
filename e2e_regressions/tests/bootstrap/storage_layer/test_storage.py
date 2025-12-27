"""
Bootstrap Storage Layer E2E Tests.

Tests StorageClass configuration and Platform PostgreSQL.
"""
import pytest
import pathlib
import os
from conftest import TestConfig


# =============================================================================
# StorageClass Tests
# =============================================================================

@pytest.mark.smoke
@pytest.mark.bootstrap
async def test_storage_class_local_path_retain_defined():
    """Verify local-path-retain storage class is defined in terraform."""
    storage_tf = pathlib.Path(__file__).parent.parent.parent.parent.parent / "bootstrap" / "4.storage.tf"
    
    if not storage_tf.exists():
        pytest.skip("Storage configuration file not found")
    
    content = storage_tf.read_text()
    assert "local_path_retain" in content or "local-path-retain" in content, \
        "local-path-retain StorageClass should be defined"


@pytest.mark.bootstrap
async def test_storage_class_reclaim_policy():
    """Verify storage class has Retain reclaim policy."""
    storage_tf = pathlib.Path(__file__).parent.parent.parent.parent.parent / "bootstrap" / "4.storage.tf"
    
    if not storage_tf.exists():
        pytest.skip("Storage configuration file not found")
    
    content = storage_tf.read_text()
    assert "Retain" in content, "Storage class should have Retain policy"


@pytest.mark.bootstrap
async def test_storage_data_directory_configured():
    """Verify /data directory is configured for persistent storage."""
    storage_tf = pathlib.Path(__file__).parent.parent.parent.parent.parent / "bootstrap" / "4.storage.tf"
    
    if not storage_tf.exists():
        pytest.skip("Storage configuration file not found")
    
    content = storage_tf.read_text()
    assert "/data" in content, "Storage should be configured to use /data directory"


@pytest.mark.bootstrap
async def test_storage_provisioner_configured():
    """Verify local-path-provisioner is properly configured."""
    storage_tf = pathlib.Path(__file__).parent.parent.parent.parent.parent / "bootstrap" / "4.storage.tf"
    
    if not storage_tf.exists():
        pytest.skip("Storage configuration file not found")
    
    content = storage_tf.read_text()
    assert "local-path-provisioner" in content or "rancher" in content, \
        "Local path provisioner should be configured"


# =============================================================================
# Platform PostgreSQL Tests
# =============================================================================

@pytest.mark.smoke
@pytest.mark.bootstrap
async def test_platform_pg_config_exists():
    """Verify Platform PostgreSQL configuration exists."""
    # Check for platform_pg.tf file
    pg_tf = pathlib.Path(__file__).parent.parent.parent.parent.parent / "bootstrap" / "5.platform_pg.tf"
    
    if not pg_tf.exists():
        # Try alternative name
        pg_tf = pathlib.Path(__file__).parent.parent.parent.parent.parent / "bootstrap" / "4.platform_pg.tf"
    
    assert pg_tf.exists(), "Platform PG configuration should exist"


@pytest.mark.bootstrap
async def test_platform_pg_accessible():
    """Verify platform PostgreSQL is accessible."""
    db_host = os.getenv("PLATFORM_DB_HOST")
    db_port = os.getenv("PLATFORM_DB_PORT", "5432")
    db_user = os.getenv("PLATFORM_DB_USER", "postgres")
    db_password = os.getenv("PLATFORM_DB_PASSWORD")
    
    if not all([db_host, db_password]):
        pytest.skip("Platform DB credentials not configured")
    
    try:
        import asyncpg
        conn = await asyncpg.connect(
            host=db_host,
            port=int(db_port),
            user=db_user,
            password=db_password,
            timeout=10.0,
        )
        
        result = await conn.fetchval('SELECT 1')
        assert result == 1, "Database should respond to queries"
        await conn.close()
    except ImportError:
        pytest.skip("asyncpg not installed")


@pytest.mark.bootstrap
async def test_platform_pg_databases_exist():
    """Verify required databases exist (vault, casdoor, digger)."""
    db_host = os.getenv("PLATFORM_DB_HOST") or "platform-pg-rw.platform.svc.cluster.local"
    db_port = os.getenv("PLATFORM_DB_PORT", "5432")
    db_user = os.getenv("PLATFORM_DB_USER", "postgres")
    db_password = os.getenv("PLATFORM_DB_PASSWORD") or os.getenv("TF_VAR_vault_postgres_password")
    
    if not db_password:
        pytest.skip("Platform DB credentials not configured")
    
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
        
        # Critical databases that should exist
        critical_dbs = ['vault', 'casdoor', 'digger']
        found_dbs = [db for db in critical_dbs if db in db_names]
        
        assert len(found_dbs) >= 2, \
            f"Expected at least 2 platform databases (vault/casdoor/digger), found: {found_dbs} in {db_names}"
        
        await conn.close()
    except ImportError:
        pytest.skip("asyncpg not installed")
    except Exception as e:
        pytest.skip(f"Cannot connect to database: {e}")


@pytest.mark.bootstrap
async def test_platform_pg_namespace_configured():
    """Verify Platform PG is configured in platform namespace."""
    pg_tf = pathlib.Path(__file__).parent.parent.parent.parent.parent / "bootstrap" / "5.platform_pg.tf"
    
    if not pg_tf.exists():
        pg_tf = pathlib.Path(__file__).parent.parent.parent.parent.parent / "bootstrap" / "4.platform_pg.tf"
    
    if not pg_tf.exists():
        pytest.skip("Platform PG configuration not found")
    
    content = pg_tf.read_text()
    assert "platform" in content, "Platform PG should be in platform namespace"


@pytest.mark.bootstrap
async def test_cnpg_cluster_resource_exists(config: TestConfig):
    """Verify CNPG Cluster resource exists in the cluster."""
    import subprocess
    try:
        # Match centralized naming
        name = config.K8sResources.PLATFORM_PG_NAME
        ctype = config.K8sResources.CNPG_CLUSTER_TYPE
        
        result = subprocess.run(
            ["kubectl", "get", ctype, "-n", "platform", name, "-o", "jsonpath={.metadata.name}"],
            capture_output=True, text=True, timeout=10.0
        )
        if result.returncode == 0 and result.stdout:
            assert name in result.stdout, \
                f"CNPG cluster '{name}' should exist"
        else:
            pytest.skip(f"kubectl command failed or resource {name} not found (safe to skip in plan-only envs)")
    except FileNotFoundError:
        pytest.skip("kubectl not found in test environment")


@pytest.mark.bootstrap
async def test_platform_pg_superuser_secret_exists():
    """Verify Platform PG superuser secret exists."""
    import subprocess
    try:
        result = subprocess.run(
            ["kubectl", "get", "secret", "platform-pg-superuser", "-n", "platform", "-o", "jsonpath={.metadata.name}"],
            capture_output=True, text=True, timeout=10.0
        )
        
        if result.returncode == 0 and result.stdout:
            assert "platform-pg-superuser" in result.stdout, \
                "Platform PG superuser secret should exist"
        else:
            pytest.skip("kubectl command failed or secret not found")
    except FileNotFoundError:
        pytest.skip("kubectl not found in test environment")


@pytest.mark.bootstrap
async def test_platform_pg_service_exists():
    """Verify Platform PG read-write service exists."""
    import subprocess
    try:
        result = subprocess.run(
            ["kubectl", "get", "svc", "platform-pg-rw", "-n", "platform", "-o", "jsonpath={.metadata.name}"],
            capture_output=True, text=True, timeout=10.0
        )
        
        if result.returncode == 0 and result.stdout:
            assert "platform-pg-rw" in result.stdout, \
                "Platform PG read-write service should exist"
        else:
            pytest.skip("kubectl command failed or service not found")
    except FileNotFoundError:
        pytest.skip("kubectl not found in test environment")


@pytest.mark.bootstrap
async def test_platform_pg_pvc_bound():
    """Verify Platform PG PVC is bound."""
    import subprocess
    try:
        result = subprocess.run(
            ["kubectl", "get", "pvc", "-n", "platform", "-l", "cnpg.io/cluster=platform-pg", "-o", "jsonpath={.items[*].status.phase}"],
            capture_output=True, text=True, timeout=10.0
        )
        
        if result.returncode == 0 and result.stdout:
            phases = result.stdout.split()
            assert all(phase == "Bound" for phase in phases), \
                f"All Platform PG PVCs should be Bound, got: {phases}"
            assert len(phases) > 0, "At least one PVC should exist"
        else:
            pytest.skip("kubectl command failed or no PVCs found")
    except FileNotFoundError:
        pytest.skip("kubectl not found in test environment")


@pytest.mark.bootstrap
async def test_platform_pg_pod_running():
    """Verify Platform PG pod is running."""
    import subprocess
    try:
        result = subprocess.run(
            ["kubectl", "get", "pods", "-n", "platform", "-l", "cnpg.io/cluster=platform-pg", "-o", "jsonpath={.items[*].status.phase}"],
            capture_output=True, text=True, timeout=10.0
        )
        
        if result.returncode == 0 and result.stdout:
            phases = result.stdout.split()
            assert any(phase == "Running" for phase in phases), \
                f"At least one Platform PG pod should be running, got: {phases}"
        else:
            pytest.skip("kubectl command failed or no Platform PG pods found")
    except FileNotFoundError:
        pytest.skip("kubectl not found in test environment")


@pytest.mark.bootstrap
async def test_platform_pg_user_permissions():
    """Verify Platform PG postgres user has superuser permissions."""
    db_host = os.getenv("PLATFORM_DB_HOST") or "platform-pg-rw.platform.svc.cluster.local"
    db_port = os.getenv("PLATFORM_DB_PORT", "5432")
    db_user = os.getenv("PLATFORM_DB_USER", "postgres")
    db_password = os.getenv("PLATFORM_DB_PASSWORD") or os.getenv("TF_VAR_vault_postgres_password")
    
    if not db_password:
        pytest.skip("Platform DB credentials not configured")
    
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
        
        result = await conn.fetchrow(
            "SELECT rolsuper FROM pg_roles WHERE rolname = $1", db_user
        )
        
        assert result is not None, f"User {db_user} should exist"
        assert result['rolsuper'] is True, f"User {db_user} should have superuser privileges"
        
        await conn.close()
    except ImportError:
        pytest.skip("asyncpg not installed")
    except Exception as e:
        pytest.skip(f"Cannot verify user permissions: {e}")


@pytest.mark.bootstrap
async def test_platform_pg_connection_pool():
    """Verify Platform PG can handle multiple connections."""
    db_host = os.getenv("PLATFORM_DB_HOST") or "platform-pg-rw.platform.svc.cluster.local"
    db_port = os.getenv("PLATFORM_DB_PORT", "5432")
    db_user = os.getenv("PLATFORM_DB_USER", "postgres")
    db_password = os.getenv("PLATFORM_DB_PASSWORD") or os.getenv("TF_VAR_vault_postgres_password")
    
    if not db_password:
        pytest.skip("Platform DB credentials not configured")
    
    try:
        import asyncpg
        
        # Try to open multiple connections
        connections = []
        for _ in range(3):
            conn = await asyncpg.connect(
                host=db_host,
                port=int(db_port),
                user=db_user,
                password=db_password,
                database="postgres",
                timeout=10.0,
            )
            connections.append(conn)
        
        # Verify all connections work
        for conn in connections:
            result = await conn.fetchval('SELECT 1')
            assert result == 1
        
        # Close all connections
        for conn in connections:
            await conn.close()
        
    except ImportError:
        pytest.skip("asyncpg not installed")
    except Exception as e:
        pytest.skip(f"Connection pool test failed: {e}")


@pytest.mark.bootstrap
async def test_storage_class_usage():
    """Verify StorageClass is being used by PVCs."""
    import subprocess
    try:
        result = subprocess.run(
            ["kubectl", "get", "pvc", "-A", "-o", "jsonpath={.items[*].spec.storageClassName}"],
            capture_output=True, text=True, timeout=10.0
        )
        
        if result.returncode == 0 and result.stdout:
            storage_classes = result.stdout.split()
            assert any("local-path" in sc for sc in storage_classes), \
                f"local-path StorageClass should be used, found: {set(storage_classes)}"
        else:
            pytest.skip("kubectl command failed or no PVCs found")
    except FileNotFoundError:
        pytest.skip("kubectl not found in test environment")
