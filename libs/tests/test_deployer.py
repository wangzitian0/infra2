"""Unit tests for deployer safety checks."""

from unittest.mock import MagicMock
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_make_tasks_sync_surfaces_failed_action_as_nonzero_exit() -> None:
    """A 'failed' action must raise (non-zero exit) so the iac-runner sees a real
    failure instead of a green '✅ sync completed' — the silent-deploy root cause."""
    from invoke.exceptions import Exit
    from libs.deployer import Deployer, make_tasks

    class FailingDeployer(Deployer):
        service = "demo"

        @classmethod
        def sync(cls, c, force=False):
            return {"action": "failed", "details": "1Password to Vault sync failed"}

    tasks = make_tasks(FailingDeployer, MagicMock())
    with pytest.raises(Exit):
        tasks["sync"].body(MagicMock(), force=False)


def test_make_tasks_sync_keeps_skipped_action_as_success() -> None:
    """'skipped' — including the deliberate fail-closed skip — stays a success
    (no raise), preserving the load-shedding safety net unchanged."""
    from libs.deployer import Deployer, make_tasks

    class SkippingDeployer(Deployer):
        service = "demo"

        @classmethod
        def sync(cls, c, force=False):
            return {
                "action": "skipped",
                "details": "Remote config unreadable; fail-closed",
            }

    tasks = make_tasks(SkippingDeployer, MagicMock())
    result = tasks["sync"].body(MagicMock())
    assert result["action"] == "skipped"


def test_prepare_dirs_reasserts_subpath_ownership_after_blanket_chown(
    monkeypatch,
) -> None:
    """A `data_subpath_uids` island (e.g. op-ch ClickHouse = 101) must be chowned AFTER
    the blanket `chown -R {uid}`, so a uid-1000 service tree never clobbers its embedded
    uid-101 ClickHouse back to 1000 — the silent openpanel ingestion outage."""
    import libs.deployer as d

    cmds: list[str] = []
    monkeypatch.setattr(d, "validate_env", lambda: [])
    monkeypatch.setattr(d, "run_with_status", lambda c, cmd, label: cmds.append(cmd))

    class OPDeployer(d.Deployer):
        service = "openpanel"
        uid = "1000"
        gid = "1000"
        data_subpath_uids = {"op-ch": ("101", "101")}

        @classmethod
        def env(cls):
            return {"ENV": "production", "VPS_HOST": "vps"}

        @classmethod
        def data_path_for_env(cls, e):
            return "/data/platform/openpanel"

    assert OPDeployer._prepare_dirs(MagicMock()) is True

    blanket = next(
        i
        for i, cmd in enumerate(cmds)
        if "chown -R 1000:1000 /data/platform/openpanel'" in cmd
    )
    opch = next(
        i
        for i, cmd in enumerate(cmds)
        if "chown -R 101:101 /data/platform/openpanel/op-ch" in cmd
    )
    assert opch > blanket, (
        "op-ch (uid 101) chown must run AFTER the blanket uid-1000 chown"
    )


def test_sync_skips_prod_only_service_on_non_production() -> None:
    """prod_only services (observability/analytics: signoz, clickhouse, openpanel)
    are never deployed to non-production envs — sync() short-circuits to 'skipped'
    before any vault/dokploy work, so a staging copy is never created."""
    from libs.deployer import Deployer

    class ObsDeployer(Deployer):
        service = "signoz"
        prod_only = True

        @classmethod
        def env(cls):
            return {"ENV": "staging"}

    skipped = ObsDeployer.sync(MagicMock(), force=False)
    assert skipped["action"] == "skipped"
    assert "prod-only" in skipped["details"]

    # On production the prod-only short-circuit must NOT fire (it deploys normally;
    # here it proceeds past the check and fails later on missing test env, proving
    # the skip is non-production-only — not a blanket skip).
    class ObsDeployerProd(ObsDeployer):
        @classmethod
        def env(cls):
            return {"ENV": "production"}

    prod = ObsDeployerProd.sync(MagicMock(), force=False)
    assert not (prod["action"] == "skipped" and "prod-only" in prod.get("details", ""))


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


def test_preserve_runtime_env_keeps_existing_approle_creds() -> None:
    """Infra-011.3 / #369: AppRole creds (VAULT_ROLE_ID / VAULT_SECRET_ID) injected
    out-of-band must survive a redeploy that regenerates the git-derived env, or the
    migrated vault-agent loses them and crash-loops. The legacy static VAULT_APP_TOKEN
    is no longer preserved (it is cleaned up on the next redeploy)."""
    from libs.deployer import _preserve_runtime_env

    result = _preserve_runtime_env(
        "ENV=production\nINTERNAL_DOMAIN=zitian.party",
        "ENV=old\nVAULT_ROLE_ID=role-abc\nVAULT_SECRET_ID=secret-xyz\n"
        "VAULT_APP_TOKEN=hvs.legacy\nSTALE=value",
    )

    assert result.splitlines() == [
        "ENV=production",
        "INTERNAL_DOMAIN=zitian.party",
        "VAULT_ROLE_ID=role-abc",
        "VAULT_SECRET_ID=secret-xyz",
    ]
    assert "VAULT_APP_TOKEN" not in result


def test_parse_env_text_ignores_comments_blanks_and_malformed_lines() -> None:
    from libs.deployer import _parse_env_text

    assert _parse_env_text("\n# comment\nA=1\nINVALID\nB=two=parts\n") == {
        "A": "1",
        "B": "two=parts",
    }


def test_data_path_for_env_requires_isolation_for_non_production(monkeypatch) -> None:
    from libs.deployer import Deployer

    class DummyDeployer(Deployer):
        data_path = "/data/app"

    monkeypatch.delenv("ALLOW_SHARED_DATA_PATH", raising=False)

    with pytest.raises(ValueError, match="Non-production requires DATA_PATH"):
        DummyDeployer.data_path_for_env({"ENV": "staging", "PROJECT": "platform"})


def test_data_path_for_env_allows_explicit_suffix_and_override(monkeypatch) -> None:
    from libs.deployer import Deployer

    class DummyDeployer(Deployer):
        data_path = "/data/app"

    monkeypatch.delenv("ALLOW_SHARED_DATA_PATH", raising=False)

    assert (
        DummyDeployer.data_path_for_env(
            {"ENV": "staging", "PROJECT": "platform", "ENV_SUFFIX": "-staging"}
        )
        == "/data/app-staging"
    )
    assert (
        DummyDeployer.data_path_for_env(
            {"ENV": "staging", "PROJECT": "platform", "DATA_PATH": "/data/custom"}
        )
        == "/data/custom"
    )


def test_compose_env_base_filters_empty_optional_values() -> None:
    from libs.deployer import Deployer

    class DummyDeployer(Deployer):
        data_path = "/data/app"

    result = DummyDeployer.compose_env_base(
        {
            "ENV": "production",
            "INTERNAL_DOMAIN": "zitian.party",
            "ENV_DOMAIN_SUFFIX": None,
        }
    )

    assert result == {
        "ENV": "production",
        "INTERNAL_DOMAIN": "zitian.party",
        "DATA_PATH": "/data/app",
    }


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


def test_compose_artifact_files_handles_json_copy_and_bind_mounts(tmp_path) -> None:
    from libs.deployer import _compose_artifact_files

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "main.py").write_text("print('hello')\n", encoding="utf-8")
    config_file = tmp_path / "config.yaml"
    config_file.write_text("enabled: true\n", encoding="utf-8")
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        'FROM python:3.12-slim\nCOPY ["app/main.py", "/app/main.py"]\n',
        encoding="utf-8",
    )
    compose = tmp_path / "compose.yaml"
    compose_content = """
services:
  app:
    build:
      context: .
      dockerfile: Dockerfile
    volumes:
      - type: bind
        source: ./config.yaml
        target: /etc/app/config.yaml
      - type: volume
        source: named-volume
        target: /data
"""
    compose.write_text(compose_content, encoding="utf-8")

    files = _compose_artifact_files(str(compose), compose_content)

    assert files == sorted({dockerfile, app_dir / "main.py", config_file})


def test_compose_artifact_files_ignores_invalid_yaml(tmp_path) -> None:
    from libs.deployer import _compose_artifact_files

    compose = tmp_path / "compose.yaml"

    assert _compose_artifact_files(str(compose), "services: [") == []


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


def test_deployment_ids_accepts_id_or_deployment_id() -> None:
    from libs.deployer import Deployer

    assert Deployer._deployment_ids(
        [{"deploymentId": "deploy-1"}, {"id": "deploy-2"}, {"deploymentId": ""}, {}]
    ) == {"deploy-1", "deploy-2"}


def test_get_compose_deployments_filters_malformed_records() -> None:
    from libs.deployer import Deployer

    class Client:
        def get_compose_deployments(self, compose_id):
            assert compose_id == "compose-1"
            return [{"deploymentId": "ok"}, "bad", None]

    assert Deployer._get_compose_deployments(Client(), "compose-1") == [
        {"deploymentId": "ok"}
    ]


def test_wait_for_new_deployment_record_fails_on_error_status(monkeypatch) -> None:
    import libs.deployer as deployer
    from libs.deployer import Deployer

    monkeypatch.setattr(deployer.time, "monotonic", lambda: 100.0)

    class Client:
        def get_compose_deployments(self, compose_id):
            return [{"deploymentId": "new", "status": "error"}]

    with pytest.raises(RuntimeError, match="entered error"):
        Deployer._wait_for_new_deployment_record(
            Client(), "compose-1", {"old"}, timeout_seconds=0, interval_seconds=1
        )


def test_wait_for_new_deployment_record_accepts_success_status(monkeypatch) -> None:
    import libs.deployer as deployer
    from libs.deployer import Deployer

    monkeypatch.setattr(deployer.time, "monotonic", lambda: 100.0)

    class Client:
        def get_compose_deployments(self, compose_id):
            return [{"id": "new", "status": "successful"}]

    assert (
        Deployer._wait_for_new_deployment_record(
            Client(), "compose-1", {"old"}, timeout_seconds=0, interval_seconds=1
        )
        is True
    )


def test_wait_for_new_deployment_record_times_out_on_unknown_status(
    monkeypatch,
) -> None:
    import libs.deployer as deployer
    from libs.deployer import Deployer

    monkeypatch.setattr(deployer.time, "monotonic", lambda: 100.0)

    class Client:
        def get_compose_deployments(self, compose_id):
            return [{"deploymentId": "new", "status": "queued"}]

    assert (
        Deployer._wait_for_new_deployment_record(
            Client(), "compose-1", {"old"}, timeout_seconds=0, interval_seconds=1
        )
        is False
    )


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

    def test_verify_vault_app_token_skips_for_approle_service(self, monkeypatch):
        """#369: a vestigial VAULT_APP_TOKEN on an AppRole service must not gate deploys —
        it would expire un-renewed and hard-block a redeploy that would clean it up."""
        import libs.deployer as deployer
        import libs.dokploy as dokploy

        dummy = self._deployer()

        class _Client:
            def find_compose_by_name(self, *_a, **_k):
                return {
                    "env": "VAULT_ROLE_ID=r\nVAULT_SECRET_ID=s\nVAULT_APP_TOKEN=hvs.stale"
                }

        monkeypatch.setattr(dokploy, "get_dokploy", lambda **_k: _Client())
        monkeypatch.setattr(
            dummy,
            "env",
            classmethod(
                lambda cls: {"ENV": "production", "INTERNAL_DOMAIN": "zitian.party"}
            ),
        )

        def _boom(*_a, **_k):
            raise AssertionError("AppRole service must not hit verify_vault_token")

        monkeypatch.setattr(deployer, "verify_vault_token", _boom)

        status = dummy.verify_vault_app_token()
        assert status["valid"] is True
        assert "AppRole" in status["details"]


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


def test_await_effective_config_hash_retries_until_settled(monkeypatch):
    """Async Dokploy settling: a stale/none first read must be retried, not
    treated as a failure, once the effective hash advances."""
    import libs.deployer as deployer
    from libs.deployer import Deployer

    class D(Deployer):
        service = "x"
        compose_path = "x/compose.yaml"
        data_path = "/data/x"

    monkeypatch.setattr(deployer.time, "sleep", lambda _s: None)
    reads = iter([None, "stale", "expected"])
    monkeypatch.setattr(
        D, "get_remote_config_hash", classmethod(lambda cls: next(reads))
    )

    assert D._await_effective_config_hash("expected") == "expected"


def test_await_effective_config_hash_times_out_when_unadvanced(monkeypatch):
    """Fails closed: if the effective hash never advances within the window, the
    last (stale) value is returned so the caller reports failure."""
    import libs.deployer as deployer
    from libs.deployer import Deployer

    class D(Deployer):
        service = "x"
        compose_path = "x/compose.yaml"
        data_path = "/data/x"

    monkeypatch.setattr(deployer.time, "sleep", lambda _s: None)
    clock = iter([0.0, 999.0])  # deadline=60; next check is past it
    monkeypatch.setattr(deployer.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(D, "get_remote_config_hash", classmethod(lambda cls: "stale"))

    assert D._await_effective_config_hash("expected") == "stale"


def test_approle_preflight_passes_for_non_approle_compose(tmp_path):
    from libs.deployer import Deployer

    cf = tmp_path / "compose.yaml"
    cf.write_text("services:\n  x:\n    image: foo\n")

    class D(Deployer):
        service = "x"
        compose_path = str(cf)
        data_path = "/data/x"

    D._assert_approle_creds_present("FOO=1")  # no raise


def test_approle_preflight_passes_when_creds_present(tmp_path):
    from libs.deployer import Deployer

    cf = tmp_path / "compose.yaml"
    cf.write_text("environment:\n  - VAULT_ROLE_ID=${VAULT_ROLE_ID:-}\n")

    class D(Deployer):
        service = "x"
        compose_path = str(cf)
        data_path = "/data/x"

    # VAULT_ADDR is required too (see test below); include it so this stays a happy path.
    D._assert_approle_creds_present(
        "VAULT_ROLE_ID=r\nVAULT_SECRET_ID=s\nVAULT_ADDR=https://vault.example"
    )  # no raise


def test_approle_preflight_requires_vault_addr(tmp_path):
    """#257 follow-up: an AppRole service with role/secret but NO VAULT_ADDR must fail
    fast at deploy time — otherwise the vault-agent hangs on an empty address and the
    service deadlocks on its healthcheck (the compose has no `${VAULT_ADDR:-}` default and
    the entrypoint guards only role/secret)."""
    import pytest
    from libs.deployer import Deployer

    cf = tmp_path / "compose.yaml"
    cf.write_text(
        "environment:\n  - VAULT_ROLE_ID=${VAULT_ROLE_ID:-}\n"
        "  - VAULT_SECRET_ID=${VAULT_SECRET_ID:-}\n  - VAULT_ADDR=${VAULT_ADDR}\n"
    )

    class D(Deployer):
        service = "x"
        compose_path = str(cf)
        data_path = "/data/x"

    with pytest.raises(ValueError, match="VAULT_ADDR"):
        D._assert_approle_creds_present(
            "VAULT_ROLE_ID=r\nVAULT_SECRET_ID=s"
        )  # addr absent


def test_approle_preflight_fails_closed_when_creds_missing(tmp_path, monkeypatch):
    import pytest
    from libs.deployer import Deployer

    cf = tmp_path / "compose.yaml"
    cf.write_text(
        "environment:\n  - VAULT_ROLE_ID=${VAULT_ROLE_ID:-}\n"
        "  - VAULT_SECRET_ID=${VAULT_SECRET_ID:-}\n"
    )

    class D(Deployer):
        service = "app"
        compose_path = str(cf)
        data_path = "/data/x"
        project = "finance_report"

    monkeypatch.setattr(D, "env", classmethod(lambda cls: {"ENV": "staging"}))

    with pytest.raises(ValueError, match="VAULT_ROLE_ID"):
        D._assert_approle_creds_present("VAULT_ROLE_ID=\nVAULT_SECRET_ID=")

    try:
        D._assert_approle_creds_present("")
    except ValueError as exc:
        assert "vault.setup-approle" in str(exc)
        assert "finance_report" in str(exc)


def test_approle_preflight_propagates_unreadable_compose(tmp_path):
    """An unreadable compose must NOT silently skip the preflight (fail closed)."""
    import pytest
    from libs.deployer import Deployer

    class D(Deployer):
        service = "x"
        compose_path = str(tmp_path / "missing.yaml")
        data_path = "/data/x"

    with pytest.raises(OSError):
        D._assert_approle_creds_present("FOO=1")


def test_approle_preflight_detects_secret_id_only_compose(tmp_path, monkeypatch):
    """A compose that references only VAULT_SECRET_ID is still an AppRole service."""
    import pytest
    from libs.deployer import Deployer

    cf = tmp_path / "compose.yaml"
    cf.write_text("environment:\n  - VAULT_SECRET_ID=${VAULT_SECRET_ID:-}\n")

    class D(Deployer):
        service = "app"
        compose_path = str(cf)
        data_path = "/data/x"

    monkeypatch.setattr(D, "env", classmethod(lambda cls: {"ENV": "staging"}))
    with pytest.raises(ValueError, match="VAULT_ROLE_ID|VAULT_SECRET_ID"):
        D._assert_approle_creds_present("")


def test_await_effective_config_hash_tolerates_a_transient_read(monkeypatch):
    """A single flaky Dokploy read must NOT abort an otherwise-good deploy: the poll
    multiplies reads, so it retries past a blip and matches once the hash settles."""
    import libs.deployer as deployer
    from libs.deployer import Deployer

    class D(Deployer):
        service = "x"
        compose_path = "x/compose.yaml"
        data_path = "/data/x"

    monkeypatch.setattr(deployer.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def flaky(_cls):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("compose.one 503")  # transient blip
        return "expected"

    monkeypatch.setattr(D, "get_remote_config_hash", classmethod(flaky))
    assert D._await_effective_config_hash("expected") == "expected"
    assert calls["n"] == 2  # blip tolerated, retried, then matched


def test_await_effective_config_hash_raises_only_if_whole_window_failed(monkeypatch):
    """If no clean read EVER lands in the window, surface the last error (don't
    silently pass) — but only at the deadline, never on a single read."""
    import libs.deployer as deployer
    from libs.deployer import Deployer

    class D(Deployer):
        service = "x"
        compose_path = "x/compose.yaml"
        data_path = "/data/x"

    monkeypatch.setattr(deployer.time, "sleep", lambda _s: None)
    clock = iter([0.0, 0.0, 999.0])  # deadline=60; second check is past it
    monkeypatch.setattr(deployer.time, "monotonic", lambda: next(clock))

    def always_boom(_cls):
        raise RuntimeError("compose.one down")

    monkeypatch.setattr(D, "get_remote_config_hash", classmethod(always_boom))
    with pytest.raises(RuntimeError, match="compose.one down"):
        D._await_effective_config_hash("expected")


def test_record_timeout_and_interval_honor_env_overrides(monkeypatch):
    """Operator knobs widen BOTH the deploy-record wait and the hash poll (they used
    to diverge: the poll ignored the env override)."""
    from libs.deployer import Deployer

    class D(Deployer):
        service = "x"
        compose_path = "x/compose.yaml"
        data_path = "/data/x"
        deployment_record_timeout_seconds = 60
        deployment_record_interval_seconds = 3

    monkeypatch.delenv("DOKPLOY_DEPLOYMENT_RECORD_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("DOKPLOY_DEPLOYMENT_RECORD_INTERVAL_SECONDS", raising=False)
    assert D._resolve_record_timeout() == 60
    assert D._resolve_record_interval() == 3

    monkeypatch.setenv("DOKPLOY_DEPLOYMENT_RECORD_TIMEOUT_SECONDS", "240")
    monkeypatch.setenv("DOKPLOY_DEPLOYMENT_RECORD_INTERVAL_SECONDS", "9")
    assert D._resolve_record_timeout() == 240
    assert D._resolve_record_interval() == 9
