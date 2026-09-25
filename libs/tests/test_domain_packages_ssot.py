"""Test suite for Phase 3 Domain Packages (SSOT Convergence).

Verifies the 4 domain packages:
1. libs.core (Service, environ, constants)
2. libs.security (store, supply, prune)
3. libs.backup (rehearsal, verification)
4. libs.observability (breakdown, watchers, probes, issue_trail)
And validates that compatibility shims preserve full backward compatibility.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from libs import common, env, secrets_supply, service_registry
from libs.backup import (
    RehearsalSpecification,
    create_rehearsal_plan,
    load_backup_inventory,
)
from libs.core import (
    DeploymentEnvironment,
    Service,
    get_environment,
    get_service,
    load_service_registry,
    with_env_suffix,
)
from libs.observability import (
    BreakdownVerdict,
    ContainerBreakdownWatcher,
    analyze_container_logs,
    reconcile_watchdog_issues,
)
from libs.security import (
    apply_secret_supply,
    create_secrets_resolver,
    generate_secret_token,
    prune_orphan_secrets,
    resolve_vault_token,
)


# ==========================================
# 1. libs.core Tests
# ==========================================


def test_core_service_entity_properties() -> None:
    """Test Service domain entity constructor and derived properties."""
    svc = Service(
        id="test/app",
        project="test",
        service="app",
        directory=Path("/tmp/test"),
    )
    assert svc.id == "test/app"
    assert svc.project == "test"
    assert svc.service == "app"
    assert svc.identity == "test/app"
    assert svc.container_name == "test-app"


def test_core_service_registry_loading() -> None:
    """C-01 & C-02: Test load_service_registry and get_service."""
    registry = load_service_registry()
    assert len(registry) >= 15
    assert "platform/postgres" in registry
    assert "truealpha/app" in registry

    # Test get_service with str and tuple
    pg = get_service("platform/postgres")
    assert pg is not None
    assert pg.id == "platform/postgres"
    assert pg.container_name == "platform-postgres"

    pg_tuple = get_service(("platform", "postgres"))
    assert pg_tuple == pg

    ta = get_service("truealpha/app")
    assert ta is not None
    assert ta.domain == "truealpha.club"
    assert len(ta.storage) == 1
    assert ta.storage[0].bucket == "truealpha-raw"


def test_core_environment_handling() -> None:
    """C-03 & C-04: Test DeploymentEnvironment and with_env_suffix."""
    deploy_env = DeploymentEnvironment(
        name="staging",
        env_suffix="-staging",
        internal_domain="zitian.party",
    )
    assert deploy_env.is_staging is True
    assert deploy_env.is_production is False
    assert with_env_suffix("platform-redis", deploy_env) == "platform-redis-staging"

    prod_env = DeploymentEnvironment(
        name="production",
        env_suffix="",
        internal_domain="zitian.party",
    )
    assert prod_env.is_production is True
    assert with_env_suffix("platform-redis", prod_env) == "platform-redis"

    curr = get_environment()
    assert isinstance(curr, DeploymentEnvironment)


# ==========================================
# 2. libs.security Tests
# ==========================================


def test_security_secret_token_generation() -> None:
    """S-01: Verify token generation uniqueness, length, and entropy."""
    t1 = generate_secret_token(32)
    t2 = generate_secret_token(32)
    assert len(t1) == 32
    assert len(t2) == 32
    assert t1 != t2


def test_security_vault_token_resolution(monkeypatch) -> None:
    """S-02: Verify vault token resolution priority."""
    monkeypatch.setenv("VAULT_TOKEN", "test-token-1")
    monkeypatch.delenv("VAULT_ROOT_TOKEN", raising=False)
    assert resolve_vault_token() == "test-token-1"

    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    monkeypatch.setenv("VAULT_ROOT_TOKEN", "test-root-token")
    assert resolve_vault_token() == "test-root-token"

    custom_env = {"VAULT_TOKEN": "custom-token"}
    assert resolve_vault_token(custom_env) == "custom-token"


def test_security_interfaces_exposed() -> None:
    """S-03, S-04, S-05: Ensure supply and prune interfaces are callable."""
    assert callable(apply_secret_supply)
    assert callable(create_secrets_resolver)
    assert callable(prune_orphan_secrets)


# ==========================================
# 3. libs.backup Tests
# ==========================================


def test_backup_inventory_and_rehearsal_spec() -> None:
    """B-01: Verify backup inventory and rehearsal specification."""
    inventory = load_backup_inventory()
    assert len(inventory) >= 10

    # Build spec for a postgres backup entry
    entry = next(e for e in inventory if "pg" in e.method)
    spec = RehearsalSpecification(
        entry=entry,
        artifact={"remote_uri": "local:/tmp/fake.dump"},
        archive_path=Path("/tmp/fake.dump"),
        target_container="test-rehearsal-container",
        pg_user="postgres",
        database="testdb",
        invariant_sql=("SELECT 1;",),
        allow_non_rehearsal_target=False,
    )
    plan = create_rehearsal_plan(spec)
    assert plan.target_container == "test-rehearsal-container"
    assert plan.database == "testdb"


# ==========================================
# 4. libs.observability Tests
# ==========================================


def test_observability_breakdown_analysis() -> None:
    """O-01: Verify container log analysis returns categorized verdicts."""
    log_sample = "2026-09-23 ERROR: connection refused to host platform-postgres:5432"
    verdict = analyze_container_logs("test-container", log_sample)
    assert isinstance(verdict, BreakdownVerdict)
    assert "connection refused" in verdict.cause.lower()

    vault_log = "Error: VAULT_ROLE_ID and VAULT_SECRET_ID are required to authenticate"
    vault_verdict = analyze_container_logs("vault-agent", vault_log)
    assert "vault approle 凭据缺失" in vault_verdict.cause.lower()


def test_observability_watcher_and_trail_aliases() -> None:
    """O-02 & O-04: Verify watcher and trail entry points."""
    import inspect

    assert inspect.isclass(ContainerBreakdownWatcher)
    assert callable(reconcile_watchdog_issues)


# ==========================================
# 5. Compatibility Shims Tests
# ==========================================


def test_backward_compatibility_shims() -> None:
    """Verify all re-exports in legacy modules work identically."""
    # service_registry shims
    assert hasattr(service_registry, "Service")
    assert hasattr(service_registry, "load_service_registry")
    assert service_registry.Service is Service

    # common shims
    assert hasattr(common, "DeploymentEnvironment")
    assert hasattr(common, "get_environment")

    # env shims
    assert hasattr(env, "generate_secret_token")
    assert hasattr(env, "resolve_vault_token")

    # secrets_supply shims
    assert hasattr(secrets_supply, "apply_secret_supply")
    assert hasattr(secrets_supply, "create_secrets_resolver")


def test_rehearsal_timeout_guard(monkeypatch) -> None:
    """Verify execute_rehearsal raises BackupRestoreError when timed out."""
    import time
    from libs.backup.rehearsal import BackupRestoreError, execute_rehearsal

    def slow_rehearsal(plan, **kwargs):
        time.sleep(0.5)
        return {}

    monkeypatch.setattr(
        "libs.backup.rehearsal.run_postgres_restore_rehearsal", slow_rehearsal
    )
    with pytest.raises(BackupRestoreError, match="timed out after 0.1s"):
        execute_rehearsal(None, timeout_seconds=0.1)  # type: ignore[arg-type]


def test_truealpha_deploy_s3_fails_closed(monkeypatch) -> None:
    """Verify truealpha deploy raises RuntimeError instead of silent warning when S3 check fails."""
    import importlib.util
    import sys
    from unittest.mock import MagicMock

    mock_minio = MagicMock()
    mock_minio.create_app_bucket = MagicMock()
    monkeypatch.setitem(sys.modules, "platform.03.minio.shared", mock_minio)

    root = Path(__file__).resolve().parents[2]
    deploy_path = root / "truealpha/truealpha/10.app/deploy.py"
    spec = importlib.util.spec_from_file_location("test_ta_deploy_s3", deploy_path)
    assert spec is not None and spec.loader is not None
    app_deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app_deploy)
    AppDeployer = app_deploy.AppDeployer

    mock_backend = MagicMock()
    mock_backend.get.side_effect = lambda k: {
        "S3_BUCKET": "test-bucket",
        "S3_ACCESS_KEY": "test-key",
        "S3_SECRET_KEY": "test-secret",
    }.get(k)
    monkeypatch.setattr(
        AppDeployer, "secrets_backend", classmethod(lambda cls, env=None: mock_backend)
    )

    def broken_ensure_bucket(*args, **kwargs):
        raise ConnectionRefusedError("MinIO connection failed")

    monkeypatch.setattr("infra2_sdk.runtime.s3.ensure_bucket", broken_ensure_bucket)

    with pytest.raises(RuntimeError, match="unreachable; failing deploy closed"):
        AppDeployer._ensure_minio_bucket(MagicMock())
