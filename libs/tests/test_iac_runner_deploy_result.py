"""Tests for IaC Runner deploy result semantics."""

from __future__ import annotations

import importlib.util
import sys
import types
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
IAC_RUNNER = ROOT / "bootstrap/06.iac_runner"


def _load_module(name: str, path: Path, monkeypatch):
    monkeypatch.setenv("GIT_REPO_URL", "https://github.com/wangzitian0/infra2")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_sync_services_returns_structured_failure_result(monkeypatch) -> None:
    """#182: sync result reports real failed service tasks."""
    sync_runner = _load_module("sync_runner_under_test", IAC_RUNNER / "sync_runner.py", monkeypatch)

    @contextmanager
    def unlocked(_path, _description):
        yield

    monkeypatch.setattr(sync_runner, "file_lock", unlocked)
    monkeypatch.setattr(sync_runner, "update_repo", lambda ref=None: True)

    def fake_run(task_name, _repo_path, deploy_env):
        return {
            "task": task_name,
            "success": task_name != "redis.sync",
            "stdout": f"{deploy_env}:{task_name}",
            "stderr": "boom" if task_name == "redis.sync" else "",
        }

    monkeypatch.setattr(sync_runner, "run_invoke_task", fake_run)

    result = sync_runner.sync_services(
        {"platform/postgres", "platform/redis", "bootstrap/vault"},
        ref="main",
        deploy_env="staging",
    )

    assert result.success is False
    assert result.succeeded == 1
    assert result.failed == 1
    assert result.skipped == 1
    payload = result.to_dict()
    assert payload["results"][0]["service"] == "bootstrap/vault"
    assert any(item["stderr"] == "boom" for item in payload["results"])


def test_sync_result_includes_actionable_failure_summary(monkeypatch) -> None:
    """#161: failed deploy responses expose one-look failure diagnostics."""
    sync_runner = _load_module(
        "sync_runner_summary_under_test", IAC_RUNNER / "sync_runner.py", monkeypatch
    )
    result = sync_runner.SyncResult(
        env="staging",
        ref="main",
        requested_services=["platform/activepieces"],
        results=[
            sync_runner.ServiceSyncResult(
                service="platform/activepieces",
                task="activepieces.sync",
                success=False,
                stderr=(
                    "libs.env.VaultSecrets.VaultSecretNotFoundError:\n"
                    "❌ Secret not found: platform/staging/activepieces\n"
                ),
            )
        ],
    )

    payload = result.to_dict()

    assert payload["failure_summary"] == [
        {
            "service": "platform/activepieces",
            "task": "activepieces.sync",
            "error_kind": "vault_secret_missing",
            "summary": "Vault secret path is missing: platform/staging/activepieces",
            "next_action": (
                "Create or repair secret/data/platform/staging/activepieces "
                "before rerunning deploy."
            ),
        }
    ]
    assert payload["results"][0]["diagnostic"]["error_kind"] == "vault_secret_missing"


def test_sync_result_truncates_large_service_output(monkeypatch) -> None:
    """#161: deploy responses keep stderr useful without flooding Actions logs."""
    sync_runner = _load_module(
        "sync_runner_truncate_under_test", IAC_RUNNER / "sync_runner.py", monkeypatch
    )
    result = sync_runner.ServiceSyncResult(
        service="platform/postgres",
        task="postgres.sync",
        success=False,
        stdout="x" * (sync_runner.MAX_RESULT_OUTPUT_CHARS + 10),
        stderr="y" * (sync_runner.MAX_RESULT_OUTPUT_CHARS + 20),
    )

    payload = result.to_dict()

    assert payload["stdout"].startswith("...<truncated 10 chars>...")
    assert payload["stderr"].startswith("...<truncated 20 chars>...")
    assert len(payload["stdout"]) < sync_runner.MAX_RESULT_OUTPUT_CHARS + 80
    assert len(payload["stderr"]) < sync_runner.MAX_RESULT_OUTPUT_CHARS + 80


def test_deploy_wait_returns_500_when_sync_fails(monkeypatch) -> None:
    """#182: /deploy wait=true exposes failed sync to GitHub Actions."""
    sync_runner = _load_module("sync_runner", IAC_RUNNER / "sync_runner.py", monkeypatch)
    fake_flask = types.ModuleType("flask")

    class FakeFlask:
        def __init__(self, _name):
            pass

        def route(self, *_args, **_kwargs):
            return lambda func: func

    fake_flask.Flask = FakeFlask
    fake_flask.jsonify = lambda payload: payload
    fake_flask.request = types.SimpleNamespace(
        headers={},
        data=b"",
        json={"env": "staging", "ref": "main", "wait": True, "triggered_by": "ci"},
    )
    monkeypatch.setitem(sys.modules, "flask", fake_flask)
    webhook_server = _load_module("webhook_server_under_test", IAC_RUNNER / "webhook_server.py", monkeypatch)

    failed = sync_runner.SyncResult(
        env="staging",
        ref="main",
        requested_services=["platform/redis"],
        results=[
            sync_runner.ServiceSyncResult(
                service="platform/redis",
                task="redis.sync",
                success=False,
                stderr="boom",
            )
        ],
    )
    monkeypatch.setattr(sync_runner, "sync_services_by_version", lambda *_args: failed)

    response = webhook_server.version_deploy()

    body, status_code = response
    assert status_code == 500
    assert body["status"] == "failed"
    assert body["result"]["failed"] == 1


def test_deploy_wait_string_false_keeps_async_path(monkeypatch) -> None:
    """#182: string wait=false must not become truthy."""
    _load_module("sync_runner", IAC_RUNNER / "sync_runner.py", monkeypatch)
    fake_flask = types.ModuleType("flask")

    class FakeFlask:
        def __init__(self, _name):
            pass

        def route(self, *_args, **_kwargs):
            return lambda func: func

    fake_flask.Flask = FakeFlask
    fake_flask.jsonify = lambda payload: payload
    fake_flask.request = types.SimpleNamespace(
        headers={},
        data=b"",
        json={"env": "staging", "ref": "main", "wait": "false", "triggered_by": "ci"},
    )
    monkeypatch.setitem(sys.modules, "flask", fake_flask)
    webhook_server = _load_module(
        "webhook_server_bool_under_test",
        IAC_RUNNER / "webhook_server.py",
        monkeypatch,
    )

    started = []

    class FakeThread:
        daemon = False

        def __init__(self, target, args):
            self.target = target
            self.args = args

        def start(self):
            started.append(self.args)

    monkeypatch.setattr(webhook_server.threading, "Thread", FakeThread)

    body = webhook_server.version_deploy()

    assert body["status"] == "accepted"
    assert body["wait"] is False
    assert started == [("staging", "main", "ci")]


def test_deploy_wait_rejects_invalid_literal(monkeypatch) -> None:
    fake_flask = types.ModuleType("flask")

    class FakeFlask:
        def __init__(self, _name):
            pass

        def route(self, *_args, **_kwargs):
            return lambda func: func

    fake_flask.Flask = FakeFlask
    fake_flask.jsonify = lambda payload: payload
    fake_flask.request = types.SimpleNamespace(
        headers={},
        data=b"",
        json={"env": "staging", "ref": "main", "wait": "sometimes"},
    )
    monkeypatch.setitem(sys.modules, "flask", fake_flask)
    webhook_server = _load_module(
        "webhook_server_invalid_bool_under_test",
        IAC_RUNNER / "webhook_server.py",
        monkeypatch,
    )

    body, status_code = webhook_server.version_deploy()

    assert status_code == 400
    assert body["error"] == "wait must be a boolean"


def test_run_invoke_task_preloads_stdlib_platform_before_repo_path(
    monkeypatch, tmp_path
) -> None:
    """Infra-011.1: repo platform/ must not shadow stdlib platform in invoke."""
    monkeypatch.delenv("PYTHONPATH", raising=False)
    sync_runner = _load_module(
        "sync_runner_platform_shadow_under_test",
        IAC_RUNNER / "sync_runner.py",
        monkeypatch,
    )
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(sync_runner.subprocess, "run", fake_run)

    result = sync_runner.run_invoke_task("postgres.sync", tmp_path, "staging")

    assert result["success"] is True
    assert captured["args"] == [
        sys.executable,
        "-P",
        "-c",
        sync_runner.INVOKE_BOOTSTRAP,
        "postgres.sync",
    ]
    assert "import platform" in sync_runner.INVOKE_BOOTSTRAP
    assert "sys.path.insert(0, '.')" in sync_runner.INVOKE_BOOTSTRAP
    assert captured["kwargs"]["cwd"] == tmp_path
    assert captured["kwargs"]["env"]["DEPLOY_ENV"] == "staging"
    assert "PYTHONPATH" not in captured["kwargs"]["env"]


def test_run_invoke_task_materializes_staging_isolation_env(
    monkeypatch, tmp_path
) -> None:
    """#161: IaC Runner child tasks receive staging isolation variables."""
    sync_runner = _load_module(
        "sync_runner_staging_env_under_test",
        IAC_RUNNER / "sync_runner.py",
        monkeypatch,
    )
    captured = {}

    def fake_run(args, **kwargs):
        captured["kwargs"] = kwargs
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(sync_runner.subprocess, "run", fake_run)

    result = sync_runner.run_invoke_task("postgres.sync", tmp_path, "staging")

    assert result["success"] is True
    invoke_env = captured["kwargs"]["env"]
    assert invoke_env["DEPLOY_ENV"] == "staging"
    assert invoke_env["ENV_SUFFIX"] == "-staging"
    assert invoke_env["ENV_DOMAIN_SUFFIX"] == "-staging"


def test_run_invoke_task_logs_safe_child_env(monkeypatch, tmp_path) -> None:
    """#161: child env diagnostics expose presence, never token values."""
    monkeypatch.setenv("VAULT_APP_TOKEN", "scoped-app-token")
    sync_runner = _load_module(
        "sync_runner_safe_env_under_test",
        IAC_RUNNER / "sync_runner.py",
        monkeypatch,
    )
    captured = {}

    def fake_run(args, **kwargs):
        captured["env"] = kwargs["env"]
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(sync_runner.subprocess, "run", fake_run)

    result = sync_runner.run_invoke_task("postgres.sync", tmp_path, "staging")

    assert result["success"] is True
    summary = sync_runner.safe_invoke_env_summary(captured["env"])
    assert "DEPLOY_ENV=staging" in summary
    assert "ENV_SUFFIX=-staging" in summary
    assert "VAULT_APP_TOKEN=set" in summary
    assert "VAULT_ROOT_TOKEN=set" in summary
    assert "scoped-app-token" not in summary


def test_run_invoke_task_materializes_production_without_suffix(
    monkeypatch, tmp_path
) -> None:
    """#161: production deploys must not get staging-style suffixes."""
    sync_runner = _load_module(
        "sync_runner_production_env_under_test",
        IAC_RUNNER / "sync_runner.py",
        monkeypatch,
    )
    captured = {}

    def fake_run(args, **kwargs):
        captured["kwargs"] = kwargs
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(sync_runner.subprocess, "run", fake_run)

    result = sync_runner.run_invoke_task("postgres.sync", tmp_path, "production")

    assert result["success"] is True
    invoke_env = captured["kwargs"]["env"]
    assert invoke_env["DEPLOY_ENV"] == "production"
    assert invoke_env["ENV_SUFFIX"] == ""
    assert invoke_env["ENV_DOMAIN_SUFFIX"] == ""


def test_deploy_env_overrides_rejects_empty_env(monkeypatch) -> None:
    """#161: empty env must not silently become production."""
    sync_runner = _load_module(
        "sync_runner_empty_env_under_test",
        IAC_RUNNER / "sync_runner.py",
        monkeypatch,
    )

    try:
        sync_runner.deploy_env_overrides("  ")
    except ValueError as exc:
        assert str(exc) == "deploy env must not be empty"
    else:
        raise AssertionError("empty deploy env should fail fast")


def test_run_invoke_task_keeps_existing_vault_root_token(monkeypatch, tmp_path) -> None:
    """#189: existing Vault root token is passed through without invoking op."""
    monkeypatch.setenv("VAULT_ROOT_TOKEN", "existing-root")
    sync_runner = _load_module(
        "sync_runner_existing_root_under_test",
        IAC_RUNNER / "sync_runner.py",
        monkeypatch,
    )
    captured = {}

    def fake_run(args, **kwargs):
        captured.setdefault("calls", []).append(args)
        captured["kwargs"] = kwargs
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(sync_runner.subprocess, "run", fake_run)

    result = sync_runner.run_invoke_task("postgres.sync", tmp_path, "staging")

    assert result["success"] is True
    assert captured["calls"] == [
        [sys.executable, "-P", "-c", sync_runner.INVOKE_BOOTSTRAP, "postgres.sync"]
    ]
    assert captured["kwargs"]["env"]["VAULT_ROOT_TOKEN"] == "existing-root"


def test_run_invoke_task_uses_scoped_vault_app_token(monkeypatch, tmp_path) -> None:
    """#191: IaC Runner uses its scoped Vault app token for sync reads."""
    monkeypatch.delenv("VAULT_ROOT_TOKEN", raising=False)
    monkeypatch.setenv("VAULT_APP_TOKEN", "scoped-app-token")
    sync_runner = _load_module(
        "sync_runner_app_token_under_test",
        IAC_RUNNER / "sync_runner.py",
        monkeypatch,
    )
    captured = {}

    def fake_run(args, **kwargs):
        captured.setdefault("calls", []).append(args)
        captured["kwargs"] = kwargs
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(sync_runner.subprocess, "run", fake_run)

    result = sync_runner.run_invoke_task("postgres.sync", tmp_path, "staging")

    assert result["success"] is True
    assert captured["calls"] == [
        [sys.executable, "-P", "-c", sync_runner.INVOKE_BOOTSTRAP, "postgres.sync"]
    ]
    assert captured["kwargs"]["env"]["VAULT_ROOT_TOKEN"] == "scoped-app-token"


def test_run_invoke_task_resolves_vault_root_token_from_1password(
    monkeypatch, tmp_path
) -> None:
    """#189: IaC Runner resolves the root token internally for sync tasks."""
    monkeypatch.delenv("VAULT_ROOT_TOKEN", raising=False)
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "op-service-token")
    sync_runner = _load_module(
        "sync_runner_op_root_under_test",
        IAC_RUNNER / "sync_runner.py",
        monkeypatch,
    )
    captured = {"calls": []}

    def fake_run(args, **kwargs):
        captured["calls"].append(args)
        if args[:2] == ["op", "read"]:
            assert args[2] == sync_runner.DEFAULT_VAULT_ROOT_TOKEN_OP_REF
            assert kwargs["env"]["OP_SERVICE_ACCOUNT_TOKEN"] == "op-service-token"
            return types.SimpleNamespace(returncode=0, stdout="resolved-root\n", stderr="")
        captured["invoke_env"] = kwargs["env"]
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(sync_runner.subprocess, "run", fake_run)

    result = sync_runner.run_invoke_task("postgres.sync", tmp_path, "staging")

    assert result["success"] is True
    assert captured["calls"] == [
        ["op", "read", sync_runner.DEFAULT_VAULT_ROOT_TOKEN_OP_REF],
        [sys.executable, "-P", "-c", sync_runner.INVOKE_BOOTSTRAP, "postgres.sync"],
    ]
    assert captured["invoke_env"]["VAULT_ROOT_TOKEN"] == "resolved-root"
