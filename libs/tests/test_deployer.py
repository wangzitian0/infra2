"""Unit tests for deployer safety checks."""

from unittest.mock import MagicMock
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class FakeSecrets:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.set_calls = []

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value
        self.set_calls.append((key, value))
        return True


class FakeDokployDeployments:
    def __init__(self, deployment_snapshots):
        self.deployment_snapshots = list(deployment_snapshots)
        self.deploy_calls = 0
        self.redeploy_calls = 0

    def deploy_compose(self, compose_id):
        assert compose_id == "compose-1"
        self.deploy_calls += 1

    def redeploy_compose(self, compose_id):
        assert compose_id == "compose-1"
        self.redeploy_calls += 1

    def get_compose(self, compose_id):
        assert compose_id == "compose-1"
        if self.deployment_snapshots:
            return {"deployments": self.deployment_snapshots.pop(0)}
        return {"deployments": []}


class FakeDokployDeploymentApi(FakeDokployDeployments):
    def __init__(self, compose_snapshots, deployment_api_snapshots):
        super().__init__(compose_snapshots)
        self.deployment_api_snapshots = list(deployment_api_snapshots)

    def get_compose_deployments(self, compose_id):
        assert compose_id == "compose-1"
        if self.deployment_api_snapshots:
            return self.deployment_api_snapshots.pop(0)
        return []


class FakeDokployDeploymentApiFailure(FakeDokployDeployments):
    def get_compose_deployments(self, compose_id):
        assert compose_id == "compose-1"
        raise RuntimeError("deployment endpoint unavailable")


def _load_deploy_module(relative_path: str, module_name: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_preserve_runtime_env_keeps_existing_vault_app_token() -> None:
    """Infra-011.3: deployer updates must not erase injected Vault app tokens."""
    from libs.deployer import _preserve_runtime_env

    result = _preserve_runtime_env(
        "ENV=production\nINTERNAL_DOMAIN=zitian.party",
        "ENV=old\nVAULT_APP_TOKEN=hvs.existing\nSTALE=value",
    )

    assert result.splitlines() == [
        "ENV=production",
        "INTERNAL_DOMAIN=zitian.party",
        "VAULT_APP_TOKEN=hvs.existing",
    ]


def test_preserve_runtime_env_keeps_explicit_new_vault_app_token() -> None:
    from libs.deployer import _preserve_runtime_env

    result = _preserve_runtime_env(
        "ENV=production\nVAULT_APP_TOKEN=hvs.new",
        "VAULT_APP_TOKEN=hvs.existing",
    )

    assert result.splitlines() == ["ENV=production", "VAULT_APP_TOKEN=hvs.new"]


def test_config_hash_includes_compose_local_mounts_and_docker_copy_sources(
    tmp_path, monkeypatch
) -> None:
    """Infra-011.8: local image/template source changes must force redeploy."""
    from libs.deployer import Deployer

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    app_file = app_dir / "app.py"
    app_file.write_text("print('v1')\n", encoding="utf-8")
    mounted_file = tmp_path / "secrets.ctmpl"
    mounted_file.write_text("TOKEN=v1\n", encoding="utf-8")
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.11-slim\nCOPY app /app\n", encoding="utf-8")
    compose = tmp_path / "compose.yaml"
    compose.write_text(
        """
services:
  app:
    build:
      context: .
      dockerfile: Dockerfile
    volumes:
      - ./secrets.ctmpl:/etc/secrets.ctmpl:ro
""",
        encoding="utf-8",
    )

    class DummyDeployer(Deployer):
        compose_path = str(compose)

    before = DummyDeployer.compute_local_config_hash(MagicMock(), {"ENV": "staging"})
    app_file.write_text("print('v2')\n", encoding="utf-8")
    after_app_change = DummyDeployer.compute_local_config_hash(
        MagicMock(), {"ENV": "staging"}
    )
    mounted_file.write_text("TOKEN=v2\n", encoding="utf-8")
    after_mount_change = DummyDeployer.compute_local_config_hash(
        MagicMock(), {"ENV": "staging"}
    )

    assert before != after_app_change
    assert after_app_change != after_mount_change


def test_deploy_compose_redeploys_when_dokploy_accepts_noop_deploy(monkeypatch):
    """Infra-011.11: deploy sync must fail fast on missing runtime deployment records."""
    import libs.deployer as deployer
    from libs.deployer import Deployer

    monkeypatch.setattr(deployer.time, "sleep", lambda _seconds: None)
    client = FakeDokployDeployments(
        [
            [{"deploymentId": "old", "status": "done"}],
            [{"deploymentId": "old", "status": "done"}],
            [{"deploymentId": "old", "status": "done"}],
            [
                {"deploymentId": "new", "status": "done"},
                {"deploymentId": "old", "status": "done"},
            ],
        ]
    )

    Deployer._deploy_compose_with_record_check(
        client,
        "compose-1",
        timeout_seconds=0,
        interval_seconds=1,
    )

    assert client.deploy_calls == 1
    assert client.redeploy_calls == 1


def test_deploy_compose_uses_deployment_api_when_compose_snapshot_is_stale(monkeypatch):
    """Infra-011.11: deploy proof must use deployment.allByCompose when available."""
    import libs.deployer as deployer
    from libs.deployer import Deployer

    monkeypatch.setattr(deployer.time, "sleep", lambda _seconds: None)
    client = FakeDokployDeploymentApi(
        compose_snapshots=[
            [{"deploymentId": "old", "status": "done"}],
            [{"deploymentId": "old", "status": "done"}],
        ],
        deployment_api_snapshots=[
            [{"deploymentId": "old", "status": "done"}],
            [
                {"deploymentId": "old", "status": "done"},
                {"deploymentId": "new", "status": "running"},
            ],
        ],
    )

    Deployer._deploy_compose_with_record_check(
        client,
        "compose-1",
        timeout_seconds=0,
        interval_seconds=1,
    )

    assert client.deploy_calls == 1
    assert client.redeploy_calls == 0


def test_deploy_compose_falls_back_when_deployment_api_fails(monkeypatch):
    """Infra-011.11: deploy proof keeps compose snapshot as compatibility fallback."""
    import libs.deployer as deployer
    from libs.deployer import Deployer

    monkeypatch.setattr(deployer.time, "sleep", lambda _seconds: None)
    client = FakeDokployDeploymentApiFailure(
        [
            [{"deploymentId": "old", "status": "done"}],
            [
                {"deploymentId": "old", "status": "done"},
                {"deploymentId": "new", "status": "running"},
            ],
        ]
    )

    Deployer._deploy_compose_with_record_check(
        client,
        "compose-1",
        timeout_seconds=0,
        interval_seconds=1,
    )

    assert client.deploy_calls == 1
    assert client.redeploy_calls == 0


def test_deploy_compose_fails_when_redeploy_has_no_runtime_record(monkeypatch):
    """Infra-011.11: deploy sync fails instead of reporting success on stale runtime."""
    import pytest
    import libs.deployer as deployer
    from libs.deployer import Deployer

    monkeypatch.setattr(deployer.time, "sleep", lambda _seconds: None)
    client = FakeDokployDeployments(
        [
            [{"deploymentId": "old", "status": "done"}],
            [{"deploymentId": "old", "status": "done"}],
            [{"deploymentId": "old", "status": "done"}],
            [{"deploymentId": "old", "status": "done"}],
        ]
    )

    with pytest.raises(RuntimeError, match="did not produce a new deployment record"):
        Deployer._deploy_compose_with_record_check(
            client,
            "compose-1",
            timeout_seconds=0,
            interval_seconds=1,
        )

    assert client.deploy_calls == 1
    assert client.redeploy_calls == 1


class TestDeployerVaultTokenPreflight:
    """Deployment sync must fail before touching runtime when Vault tokens are bad."""

    def _deployer(self):
        from libs.deployer import Deployer

        class DummyDeployer(Deployer):
            service = "app"
            project = "finance_report"
            compose_path = "finance_report/finance_report/10.app/compose.yaml"
            data_path = ""
            secret_key = ""

        return DummyDeployer

    def test_sync_fails_when_vault_token_is_invalid(self, monkeypatch):
        import libs.deployer as deployer

        dummy = self._deployer()
        monkeypatch.setattr(deployer, "validate_env", lambda: [])
        monkeypatch.setattr(
            dummy,
            "verify_vault_app_token",
            classmethod(lambda cls: {"valid": False, "details": "Token invalid"}),
        )

        result = dummy.sync(MagicMock())

        assert result["action"] == "failed"
        assert "VAULT_APP_TOKEN issue" in result["details"]
        assert "Token invalid" in result["details"]

    def test_sync_fails_when_vault_token_ttl_is_low(self, monkeypatch):
        import libs.deployer as deployer

        dummy = self._deployer()
        monkeypatch.setattr(deployer, "validate_env", lambda: [])
        monkeypatch.setattr(
            dummy,
            "verify_vault_app_token",
            classmethod(lambda cls: {"valid": True, "ttl_hours": 12}),
        )

        result = dummy.sync(MagicMock())

        assert result["action"] == "failed"
        assert "expires in 12h" in result["details"]

    def test_sync_fails_when_vault_token_cannot_be_verified(self, monkeypatch):
        import libs.deployer as deployer

        dummy = self._deployer()
        monkeypatch.setattr(deployer, "validate_env", lambda: [])

        def raise_error():
            raise RuntimeError("Vault unavailable")

        monkeypatch.setattr(
            dummy, "verify_vault_app_token", classmethod(lambda cls: raise_error())
        )

        result = dummy.sync(MagicMock())

        assert result["action"] == "failed"
        assert "Could not verify VAULT_APP_TOKEN" in result["details"]
        assert "Vault unavailable" in result["details"]


def test_minio_sync_secret_hook_repairs_root_user(monkeypatch) -> None:
    """Infra-011: sync must ensure all MinIO template fields, not only password."""
    module = _load_deploy_module("platform/03.minio/deploy.py", "minio_deploy_test")
    secrets = FakeSecrets({"root_password": "existing"})

    monkeypatch.setattr(
        module.MinioDeployer, "secrets", classmethod(lambda cls: secrets)
    )

    assert module.MinioDeployer.ensure_runtime_secrets() is True

    assert secrets.values["root_user"] == "admin"
    assert secrets.values["root_password"] == "existing"


def test_activepieces_sync_secret_hook_repairs_all_runtime_fields(monkeypatch) -> None:
    """Infra-011: sync must ensure every Activepieces secrets.ctmpl field."""
    module = _load_deploy_module(
        "platform/22.activepieces/deploy.py", "activepieces_deploy_test"
    )
    stores = {
        "postgres": FakeSecrets({"root_password": "pg"}),
        "redis": FakeSecrets({"password": "redis"}),
        "activepieces": FakeSecrets({"encryption_key": "abc"}),
    }

    monkeypatch.setattr(
        module.ActivepiecesDeployer,
        "env",
        classmethod(
            lambda cls: {
                "ENV": "staging",
                "PROJECT": "platform",
                "INTERNAL_DOMAIN": "zitian.party",
                "ENV_DOMAIN_SUFFIX": "-staging",
            }
        ),
    )
    monkeypatch.setattr(
        module,
        "get_secrets",
        lambda _project, service, _env: stores[service],
    )

    assert module.ActivepiecesDeployer.ensure_runtime_secrets() is True

    assert stores["activepieces"].values["encryption_key"] == "abc"
    assert stores["activepieces"].values["jwt_secret"]
    assert (
        stores["activepieces"].values["frontend_url"]
        == "https://automate-staging.zitian.party"
    )


def test_authentik_sync_secret_hook_repairs_bootstrap_fields(monkeypatch) -> None:
    """Infra-011: sync must keep Authentik bootstrap template fields complete."""
    module = _load_deploy_module(
        "platform/10.authentik/deploy.py", "authentik_deploy_test"
    )
    stores = {
        "postgres": FakeSecrets({"root_password": "pg"}),
        "redis": FakeSecrets({"password": "redis"}),
        "authentik": FakeSecrets({}),
    }

    monkeypatch.setattr(
        module.AuthentikDeployer,
        "env",
        classmethod(
            lambda cls: {
                "ENV": "staging",
                "PROJECT": "platform",
                "ADMIN_EMAIL": "admin@example.test",
            }
        ),
    )
    monkeypatch.setattr(
        module,
        "get_secrets",
        lambda _project, service, _env: stores[service],
    )

    assert module.AuthentikDeployer.ensure_runtime_secrets() is True

    assert stores["authentik"].values["secret_key"]
    assert stores["authentik"].values["bootstrap_password"]
    assert stores["authentik"].values["bootstrap_email"] == "admin@example.test"


def test_base_deployer_creates_missing_vault_secret_path(monkeypatch) -> None:
    """Infra-011.6: sync repairs an absent Vault path before compose deploy."""
    from libs.deployer import Deployer
    from libs.env import VaultSecrets

    class MissingPathSecrets(FakeSecrets):
        def get(self, key):
            raise VaultSecrets.VaultSecretNotFoundError("missing path")

    secrets = MissingPathSecrets()

    class DummyDeployer(Deployer):
        service = "clickhouse"
        secret_key = "password"

    monkeypatch.setattr(DummyDeployer, "secrets", classmethod(lambda cls: secrets))

    assert DummyDeployer.ensure_runtime_secrets() is True
    assert secrets.set_calls
    assert secrets.set_calls[0][0] == "password"


def test_portal_deployer_declares_no_runtime_secret_path() -> None:
    """Infra-011.6: portal sync must not read a Vault path it does not consume."""
    module = _load_deploy_module("platform/21.portal/deploy.py", "portal_deploy_test")

    assert module.PortalDeployer.secret_key == ""
