"""Tests for IaC Runner deploy result semantics."""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import os
import subprocess
import sys
import time
import types
from contextlib import contextmanager
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
IAC_RUNNER = ROOT / "bootstrap/06.iac_runner"
DEPLOY_PLATFORM_WORKFLOW = ROOT / ".github/workflows/deploy-platform.yml"
DEPLOY_REPORT_MAIN_WORKFLOW = ROOT / ".github/workflows/deploy-report-main.yml"
BOOTSTRAP_DEPLOY_SCRIPT = ROOT / "scripts/deploy_iac_runner_bootstrap.sh"
IAC_RUNNER_VAULT_POLICY = IAC_RUNNER / "vault-policy.hcl"
DEPLOY_SHA = "a" * 40
_nonce_counter = 0


def _load_module(name: str, path: Path, monkeypatch):
    monkeypatch.setenv("GIT_REPO_URL", "https://github.com/wangzitian0/infra2")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _signed_headers(monkeypatch, payload: bytes = b"") -> dict[str, str]:
    global _nonce_counter
    monkeypatch.setenv("WEBHOOK_SECRET", "test-webhook-secret")
    _nonce_counter += 1
    timestamp = str(int(time.time()))
    nonce = f"test-nonce-{_nonce_counter:04d}"
    signing_input = f"{timestamp}.{nonce}.".encode() + payload
    signature = hmac.new(
        b"test-webhook-secret", signing_input, hashlib.sha256
    ).hexdigest()
    return {
        "X-Hub-Signature-256": f"sha256={signature}",
        "X-IAC-Timestamp": timestamp,
        "X-IAC-Nonce": nonce,
    }


def test_sync_services_returns_structured_failure_result(monkeypatch) -> None:
    """#182: sync result reports real failed service tasks."""
    sync_runner = _load_module(
        "sync_runner_under_test", IAC_RUNNER / "sync_runner.py", monkeypatch
    )

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
        requested_services=["platform/prefect"],
        results=[
            sync_runner.ServiceSyncResult(
                service="platform/prefect",
                task="prefect.sync",
                success=False,
                stderr=(
                    "libs.env.VaultSecrets.VaultSecretNotFoundError:\n"
                    "❌ Secret not found: platform/staging/prefect\n"
                ),
            )
        ],
    )

    payload = result.to_dict()

    assert payload["failure_summary"] == [
        {
            "service": "platform/prefect",
            "task": "prefect.sync",
            "error_kind": "vault_secret_missing",
            "summary": "Vault secret path is missing: platform/staging/prefect",
            "next_action": (
                "Create or repair secret/data/platform/staging/prefect "
                "before rerunning deploy."
            ),
        }
    ]
    assert payload["results"][0]["diagnostic"]["error_kind"] == "vault_secret_missing"


def test_sync_result_classifies_missing_python_dependency(monkeypatch) -> None:
    """Infra-011.7: missing runner dependencies get an actionable diagnostic."""
    sync_runner = _load_module(
        "sync_runner_missing_dependency_under_test",
        IAC_RUNNER / "sync_runner.py",
        monkeypatch,
    )

    diagnostic = sync_runner.diagnose_failure(
        "ModuleNotFoundError: No module named 'yaml'"
    )

    assert diagnostic == {
        "error_kind": "missing_python_dependency",
        "summary": "IaC Runner runtime is missing Python module: yaml",
        "next_action": (
            "Rebuild or redeploy bootstrap/iac-runner from the current "
            "requirements.txt, then rerun the deployment."
        ),
    }


def test_sync_result_classifies_onepassword_service_account_deleted(
    monkeypatch,
) -> None:
    """A deleted 1Password service account (op 403) gets an actionable diagnostic
    instead of a generic failure — the silent-deploy root cause we hit."""
    sync_runner = _load_module(
        "sync_runner_op_auth_under_test",
        IAC_RUNNER / "sync_runner.py",
        monkeypatch,
    )

    diagnostic = sync_runner.diagnose_failure(
        "[ERROR] (403) Forbidden (Service Account Deleted): "
        "The Service Account used in this integration has been deleted.\n"
        "alerting sync failed: 1Password to Vault sync failed"
    )

    assert diagnostic["error_kind"] == "onepassword_auth_failed"
    assert "Service Account" in diagnostic["summary"]
    assert "OP_SERVICE_ACCOUNT_TOKEN" in diagnostic["next_action"]


def test_sync_result_classifies_dokploy_auth_over_vault_red_herring(
    monkeypatch,
) -> None:
    """A wiped DOKPLOY_API_KEY surfaces as 'No GitHub provider found'. Even when
    unrelated 'permission denied'/'vault' strings share the output, it must route
    to dokploy_auth_failed — not be mis-classified as vault_permission_denied
    (the red herring that masked the empty key for a full investigation)."""
    sync_runner = _load_module(
        "sync_runner_dokploy_auth_under_test",
        IAC_RUNNER / "sync_runner.py",
        monkeypatch,
    )

    diagnostic = sync_runner.diagnose_failure(
        "WARNING: vault token lookup: permission denied on capabilities-self\n"
        "alerting sync failed: No GitHub provider found"
    )

    assert diagnostic["error_kind"] == "dokploy_auth_failed"
    assert "DOKPLOY_API_KEY" in diagnostic["next_action"]


def test_iac_runner_policy_can_repair_service_runtime_secrets() -> None:
    """Infra-011.6: deploy sync can create/update missing runtime secret fields."""
    policy = IAC_RUNNER_VAULT_POLICY.read_text(encoding="utf-8")

    for path in (
        'path "secret/data/platform/+/*"',
        'path "secret/data/finance_report/+/*"',
    ):
        block = policy.split(path, 1)[1].split("}", 1)[0]
        assert '"create"' in block
        assert '"read"' in block
        assert '"update"' in block
        assert '"list"' in block
        assert '"delete"' not in block


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
    sync_runner = _load_module(
        "sync_runner", IAC_RUNNER / "sync_runner.py", monkeypatch
    )
    fake_flask = types.ModuleType("flask")

    class FakeFlask:
        def __init__(self, _name):
            pass

        def route(self, *_args, **_kwargs):
            return lambda func: func

    fake_flask.Flask = FakeFlask
    fake_flask.jsonify = lambda payload: payload
    fake_flask.request = types.SimpleNamespace(
        headers=_signed_headers(monkeypatch),
        data=b"",
        json={"env": "staging", "ref": DEPLOY_SHA, "wait": True, "triggered_by": "ci"},
    )
    monkeypatch.setitem(sys.modules, "flask", fake_flask)
    webhook_server = _load_module(
        "webhook_server_under_test", IAC_RUNNER / "webhook_server.py", monkeypatch
    )

    failed = sync_runner.SyncResult(
        env="staging",
        ref=DEPLOY_SHA,
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
    assert "stderr" not in body["result"]["results"][0]
    assert "stdout" not in body["result"]["results"][0]


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
        headers=_signed_headers(monkeypatch),
        data=b"",
        json={
            "env": "staging",
            "ref": DEPLOY_SHA,
            "wait": "false",
            "triggered_by": "ci",
        },
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

    body, status_code = webhook_server.version_deploy()

    assert status_code == 202
    assert body["status"] == "in_progress"
    assert body["status_url"] == "/deploy/status"
    assert body["wait"] is False
    assert started == [("staging", DEPLOY_SHA, "ci")]


def test_async_deploy_status_reports_completed_result(monkeypatch) -> None:
    """Infra-011.1: Actions can poll real deploy results without a long request."""
    sync_runner = _load_module(
        "sync_runner", IAC_RUNNER / "sync_runner.py", monkeypatch
    )
    fake_flask = types.ModuleType("flask")

    class FakeFlask:
        def __init__(self, _name):
            pass

        def route(self, *_args, **_kwargs):
            return lambda func: func

    fake_flask.Flask = FakeFlask
    fake_flask.jsonify = lambda payload: payload
    fake_request = types.SimpleNamespace(
        headers=_signed_headers(monkeypatch),
        data=b"",
        json={"env": "staging", "ref": DEPLOY_SHA, "wait": False, "triggered_by": "ci"},
    )
    fake_flask.request = fake_request
    monkeypatch.setitem(sys.modules, "flask", fake_flask)
    webhook_server = _load_module(
        "webhook_server_async_status_under_test",
        IAC_RUNNER / "webhook_server.py",
        monkeypatch,
    )
    completed = sync_runner.SyncResult(
        env="staging",
        ref=DEPLOY_SHA,
        requested_services=["platform/redis"],
        results=[
            sync_runner.ServiceSyncResult(
                service="platform/redis",
                task="redis.sync",
                success=True,
            )
        ],
    )
    monkeypatch.setattr(
        sync_runner, "sync_services_by_version", lambda *_args: completed
    )

    class ImmediateThread:
        daemon = False

        def __init__(self, target, args):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(webhook_server.threading, "Thread", ImmediateThread)

    body, status_code = webhook_server.version_deploy()

    assert status_code == 202
    assert body["status"] == "in_progress"
    fake_request.headers = _signed_headers(monkeypatch)
    fake_request.json = {"env": "staging", "ref": DEPLOY_SHA, "triggered_by": "ci"}
    status_body, status_code = webhook_server.deployment_status()

    assert status_code == 200
    assert status_body["status"] == "completed"
    assert status_body["result"]["succeeded"] == 1
    assert status_body["result"]["failed"] == 0
    assert "stdout" not in status_body["result"]["results"][0]
    assert "stderr" not in status_body["result"]["results"][0]


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
        headers=_signed_headers(monkeypatch),
        data=b"",
        json={"env": "staging", "ref": DEPLOY_SHA, "wait": "sometimes"},
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


def test_deploy_rejects_mutable_ref(monkeypatch) -> None:
    """Infra-011.10: /deploy must only accept immutable commit SHAs."""
    fake_flask = types.ModuleType("flask")

    class FakeFlask:
        def __init__(self, _name):
            self.config = {}

        def route(self, *_args, **_kwargs):
            return lambda func: func

    fake_flask.Flask = FakeFlask
    fake_flask.jsonify = lambda payload: payload
    fake_flask.request = types.SimpleNamespace(
        headers=_signed_headers(monkeypatch),
        data=b"",
        json={"env": "staging", "ref": "main", "wait": False},
    )
    monkeypatch.setitem(sys.modules, "flask", fake_flask)
    webhook_server = _load_module(
        "webhook_server_mutable_ref_under_test",
        IAC_RUNNER / "webhook_server.py",
        monkeypatch,
    )

    body, status_code = webhook_server.version_deploy()

    assert status_code == 400
    assert body["error"] == "Ref must be an exact 40-character commit SHA"


def test_health_reports_missing_runtime_dependency(monkeypatch, tmp_path) -> None:
    """Infra-011.7: /health fails before deploy when runner dependencies drift."""
    fake_flask = types.ModuleType("flask")

    class FakeFlask:
        def __init__(self, _name):
            pass

        def route(self, *_args, **_kwargs):
            return lambda func: func

    fake_flask.Flask = FakeFlask
    fake_flask.jsonify = lambda payload: payload
    fake_flask.request = types.SimpleNamespace(headers={}, data=b"", json={})
    monkeypatch.setitem(sys.modules, "flask", fake_flask)
    webhook_server = _load_module(
        "webhook_server_health_deps_under_test",
        IAC_RUNNER / "webhook_server.py",
        monkeypatch,
    )
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("VAULT_APP_TOKEN=redacted\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    (workspace / "infra2").mkdir(parents=True)
    monkeypatch.setattr(webhook_server, "SECRETS_FILE", secrets_file)
    monkeypatch.setattr(webhook_server, "WORKSPACE", workspace)
    monkeypatch.setattr(
        webhook_server,
        "_dependency_checks",
        lambda: {"python:PyYAML": False, "binary:op": True},
    )

    body, status_code = webhook_server.health()

    assert status_code == 503
    assert body["status"] == "degraded"
    assert body["checks"]["python:PyYAML"] is False


def test_health_requires_runner_process_service_account_token(
    monkeypatch, tmp_path
) -> None:
    """Infra-011.2: rendered secrets are insufficient if the process env is stale."""
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    fake_flask = types.ModuleType("flask")

    class FakeFlask:
        def __init__(self, _name):
            pass

        def route(self, *_args, **_kwargs):
            return lambda func: func

    fake_flask.Flask = FakeFlask
    fake_flask.jsonify = lambda payload: payload
    fake_flask.request = types.SimpleNamespace(headers={}, data=b"", json={})
    monkeypatch.setitem(sys.modules, "flask", fake_flask)
    webhook_server = _load_module(
        "webhook_server_health_op_token_under_test",
        IAC_RUNNER / "webhook_server.py",
        monkeypatch,
    )
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("OP_SERVICE_ACCOUNT_TOKEN=rendered\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    (workspace / "infra2").mkdir(parents=True)
    monkeypatch.setattr(webhook_server, "SECRETS_FILE", secrets_file)
    monkeypatch.setattr(webhook_server, "WORKSPACE", workspace)
    monkeypatch.setattr(
        webhook_server,
        "_dependency_checks",
        lambda: {"python:PyYAML": True, "binary:op": True},
    )

    body, status_code = webhook_server.health()

    assert status_code == 503
    assert body["checks"]["op_service_account_token"] is False


def test_vault_audit_task_import_does_not_require_pyyaml() -> None:
    """Infra-011.7: optional audit inventory parsing cannot break invoke startup."""
    script = """
import builtins
import sys
import types

original_import = builtins.__import__

def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "yaml":
        raise ModuleNotFoundError("No module named 'yaml'", name="yaml")
    return original_import(name, globals, locals, fromlist, level)

builtins.__import__ = guarded_import
fake_invoke = types.ModuleType("invoke")
fake_invoke.Exit = type("Exit", (Exception,), {})
fake_invoke.task = lambda func=None, **_kwargs: (
    (lambda wrapped: wrapped) if func is None else func
)
sys.modules["invoke"] = fake_invoke

import tools.vault_audit
assert hasattr(tools.vault_audit, "self_refresh")
"""
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    result = subprocess.run(
        [sys.executable, "-P", "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


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
    """#161: child env diagnostics expose presence, never secret values.

    AppRole creds (role_id/secret_id) are secret-equivalent and must be masked
    just like tokens.
    """
    monkeypatch.delenv("VAULT_ROOT_TOKEN", raising=False)
    monkeypatch.delenv("VAULT_APP_TOKEN", raising=False)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.example")
    monkeypatch.setenv("VAULT_ROLE_ID", "role-abc")
    monkeypatch.setenv("VAULT_SECRET_ID", "secret-xyz")
    sync_runner = _load_module(
        "sync_runner_safe_env_under_test",
        IAC_RUNNER / "sync_runner.py",
        monkeypatch,
    )
    captured = {}

    def fake_run(args, **kwargs):
        if args[:3] == ["vault", "write", "-format=json"]:
            return types.SimpleNamespace(
                returncode=0,
                stdout='{"auth": {"client_token": "bounded-deploy-token"}}',
                stderr="",
            )
        captured["env"] = kwargs["env"]
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(sync_runner.subprocess, "run", fake_run)

    result = sync_runner.run_invoke_task("postgres.sync", tmp_path, "staging")

    assert result["success"] is True
    summary = sync_runner.safe_invoke_env_summary(captured["env"])
    assert "DEPLOY_ENV=staging" in summary
    assert "ENV_SUFFIX=-staging" in summary
    assert "VAULT_ROLE_ID=set" in summary
    assert "VAULT_SECRET_ID=set" in summary
    assert "VAULT_ROOT_TOKEN=set" in summary
    assert "role-abc" not in summary
    assert "secret-xyz" not in summary


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


def test_run_invoke_task_logs_in_with_approle(monkeypatch, tmp_path) -> None:
    """#369 / Infra-011.11: deploy credential is an AppRole login, not a static token.

    iac-runner reads VAULT_ROLE_ID/VAULT_SECRET_ID (Dokploy-injected env) and runs
    `vault write auth/approle/login` to mint a short-TTL bounded token, handed to
    the child as VAULT_ROOT_TOKEN. secret_id goes via stdin, never argv; the path
    never touches 1Password.
    """
    monkeypatch.delenv("VAULT_ROOT_TOKEN", raising=False)
    monkeypatch.delenv("VAULT_APP_TOKEN", raising=False)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.example")
    monkeypatch.setenv("VAULT_ROLE_ID", "role-abc")
    monkeypatch.setenv("VAULT_SECRET_ID", "secret-xyz")
    sync_runner = _load_module(
        "sync_runner_approle_under_test",
        IAC_RUNNER / "sync_runner.py",
        monkeypatch,
    )
    captured = {"calls": []}

    def fake_run(args, **kwargs):
        captured["calls"].append(args)
        if args[:2] == ["op", "read"]:
            raise AssertionError("AppRole login must not read from 1Password")
        if args[:3] == ["vault", "write", "-format=json"]:
            captured["login_args"] = args
            captured["login_input"] = kwargs.get("input")
            return types.SimpleNamespace(
                returncode=0,
                stdout='{"auth": {"client_token": "bounded-deploy-token"}}',
                stderr="",
            )
        captured["invoke_env"] = kwargs["env"]
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(sync_runner.subprocess, "run", fake_run)

    result = sync_runner.run_invoke_task("postgres.sync", tmp_path, "staging")

    assert result["success"] is True
    login_args = captured["login_args"]
    assert "auth/approle/login" in login_args
    assert "role_id=role-abc" in login_args
    # secret_id is passed via stdin (secret_id=-), never exposed in argv.
    assert "secret_id=-" in login_args
    assert all("secret-xyz" not in part for part in login_args)
    assert captured["login_input"] == "secret-xyz"
    # The minted bounded token is handed to the child as VAULT_ROOT_TOKEN.
    assert captured["invoke_env"]["VAULT_ROOT_TOKEN"] == "bounded-deploy-token"


def test_run_invoke_task_drops_vault_app_token_fallback(monkeypatch, tmp_path) -> None:
    """#369 / Infra-011.12: the legacy VAULT_APP_TOKEN fallback is removed.

    A leftover VAULT_APP_TOKEN with no AppRole creds must NOT become the child's
    VAULT_ROOT_TOKEN, and must not trigger a Vault login.
    """
    monkeypatch.delenv("VAULT_ROOT_TOKEN", raising=False)
    monkeypatch.delenv("VAULT_ROLE_ID", raising=False)
    monkeypatch.delenv("VAULT_SECRET_ID", raising=False)
    monkeypatch.setenv("VAULT_APP_TOKEN", "scoped-app-token")
    sync_runner = _load_module(
        "sync_runner_no_fallback_under_test",
        IAC_RUNNER / "sync_runner.py",
        monkeypatch,
    )
    captured = {"calls": []}

    def fake_run(args, **kwargs):
        captured["calls"].append(args)
        if args[:1] == ["vault"]:
            raise AssertionError("No AppRole creds -> no Vault login expected")
        captured["invoke_env"] = kwargs["env"]
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(sync_runner.subprocess, "run", fake_run)

    result = sync_runner.run_invoke_task("postgres.sync", tmp_path, "staging")

    assert result["success"] is True
    assert "VAULT_ROOT_TOKEN" not in captured["invoke_env"]


def test_run_invoke_task_does_not_resolve_vault_root_token_from_1password(
    monkeypatch, tmp_path
) -> None:
    """Infra-011.10: IaC Runner must not resolve root tokens from 1Password."""
    monkeypatch.delenv("VAULT_ROOT_TOKEN", raising=False)
    monkeypatch.delenv("VAULT_APP_TOKEN", raising=False)
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
            raise AssertionError("IaC Runner must not read Vault root token via op")
        captured["invoke_env"] = kwargs["env"]
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(sync_runner.subprocess, "run", fake_run)

    result = sync_runner.run_invoke_task("postgres.sync", tmp_path, "staging")

    assert result["success"] is True
    assert captured["calls"] == [
        [sys.executable, "-P", "-c", sync_runner.INVOKE_BOOTSTRAP, "postgres.sync"],
    ]
    assert "VAULT_ROOT_TOKEN" not in captured["invoke_env"]


def test_deploy_platform_triggers_on_iac_runner_bootstrap_changes() -> None:
    """Infra-011.8: runner source changes enter the bootstrap-update workflow (via push paths)."""
    workflow = DEPLOY_PLATFORM_WORKFLOW.read_text(encoding="utf-8")

    assert '"bootstrap/06.iac_runner/**"' in workflow
    assert "scripts/deploy_iac_runner_bootstrap.sh" in workflow
    assert "Deploy IaC Runner bootstrap changes" in workflow
    assert "Check IaC Runner health" in workflow


def test_deploy_platform_no_longer_triggers_platform_service_deploys() -> None:
    """#370 cutover: deploy-platform.yml updates ONLY the iac_runner bootstrap; platform
    SERVICE deploys moved to deploy_v2. The old signed /deploy trigger + the platform/** /
    finance_report/** auto-deploy push paths must be gone."""
    workflow = DEPLOY_PLATFORM_WORKFLOW.read_text(encoding="utf-8")

    # the removed [B] trigger CODE (these strings only ever lived in that step, never a comment)
    assert "send_signed_json" not in workflow
    assert "/deploy/status" not in workflow
    assert "X-IAC-Nonce" not in workflow
    assert "X-Hub-Signature-256" not in workflow
    assert "- name: Trigger IaC Runner" not in workflow
    # the auto-deploy push paths (quoted as they appear in a YAML paths list) are gone
    assert '"platform/**"' not in workflow
    assert '"finance_report/**"' not in workflow
    assert '"libs/**"' not in workflow
    assert '"finance/**"' not in workflow


def test_report_branch_main_auto_target_is_fully_wired() -> None:
    """Infra-015: report-branch-main is the ONE auto target. The infra2 RECEIVER must fire on
    the cross-repo repository_dispatch (not manual-only) and deploy app main via deploy_v2.
    The app-repo SENDER lives in finance_report; here we lock the receiver half so a future edit
    cannot silently revert it to a manual-only / TODO stub."""
    spec = yaml.safe_load(DEPLOY_REPORT_MAIN_WORKFLOW.read_text(encoding="utf-8"))

    # receiver fires on the cross-repo dispatch the app-repo sender emits. Assert on the
    # PARSED trigger set, not a raw substring a comment / the workflow name could satisfy.
    # PyYAML parses the `on:` key as the boolean True (YAML 1.1), so accept either.
    triggers = spec.get("on", spec.get(True))
    trigger_names = set(triggers) if isinstance(triggers, (dict, list)) else {triggers}
    assert "repository_dispatch" in trigger_names, (
        "receiver must be an AUTO target (repository_dispatch), not manual-only"
    )

    # and deploys app main through the unified front door — assert on the actual run
    # scripts of the steps, not the whole file (robust to comments / unrelated wording).
    run_scripts = "\n".join(
        step["run"]
        for job in spec["jobs"].values()
        for step in job.get("steps", [])
        if isinstance(step, dict) and isinstance(step.get("run"), str)
    )
    assert "--service finance_report/app" in run_scripts
    assert "--type preview/branch" in run_scripts


def test_iac_runner_all_services_include_alerting_bridge(monkeypatch) -> None:
    """Infra-011.8: alerting bridge must not be left outside GitOps deploys."""
    sync_runner = _load_module(
        "sync_runner_alerting_under_test", IAC_RUNNER / "sync_runner.py", monkeypatch
    )

    assert sync_runner._service_task_map()["platform/alerting"] == "alerting.sync"
    assert "platform/alerting" in sync_runner._all_services()


def test_iac_runner_service_map_matches_discovered_deployers(monkeypatch) -> None:
    """Infra-011.8: every deploy.py service must be wired into GitOps."""
    from libs.deployer import discover_services

    sync_runner = _load_module(
        "sync_runner_discovery_under_test", IAC_RUNNER / "sync_runner.py", monkeypatch
    )
    discovered = discover_services()

    # SERVICE_TASK_MAP / ALL_SERVICES are now DERIVED from discover_services (Infra-013), so
    # every deploy.py service is wired by construction; assert via the lazy accessors.
    task_map = sync_runner._service_task_map()
    all_services = sync_runner._all_services()
    for service, task in discovered.items():
        assert task_map.get(service) == task
        assert service in all_services


def test_iac_runner_bootstrap_self_update_runs_before_health_check() -> None:
    """Infra-011.8: Actions updates stale runner code before checking its health."""
    workflow = DEPLOY_PLATFORM_WORKFLOW.read_text(encoding="utf-8")

    self_update = workflow.index("Deploy IaC Runner bootstrap changes")
    health_check = workflow.index("Check IaC Runner health")

    assert self_update < health_check
    assert "INFRA2_WATCHDOG_SSH_PRIVATE_KEY" in workflow


def test_iac_runner_public_health_check_retries_with_diagnostics() -> None:
    """Infra-011.8: public route convergence must not fail on one 404."""
    workflow = DEPLOY_PLATFORM_WORKFLOW.read_text(encoding="utf-8")

    assert "IaC Runner public health attempt ${attempt}/12" in workflow
    assert "for attempt in $(seq 1 12)" in workflow
    assert "--connect-timeout 10" in workflow
    assert "--max-time 20" in workflow
    assert "Last HTTP status: $last_status" in workflow
    assert "Traefik/Dokploy routing" in workflow


def test_iac_runner_bootstrap_deploy_script_is_scoped_to_runner_source() -> None:
    """Infra-011.8: self-update preserves Dokploy drift outside runner source."""
    script = BOOTSTRAP_DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert "docker inspect iac-runner" in script
    assert (
        'git -C "$code_dir" checkout -f "$INFRA2_DEPLOY_SHA" -- bootstrap/06.iac_runner'
        in script
    )
    assert "refs/heads/*:refs/remotes/origin/*" in script
    assert "docker compose \\" in script
    assert '-p "$project"' in script
    assert "--force-recreate" in script
    assert "Health check timed out for iac-runner" in script
    assert "cat /secrets/.env" not in script


def test_iac_runner_bootstrap_deploy_script_persists_dokploy_ownership() -> None:
    """Infra-011.8: Dokploy must not auto-recreate the runner behind CI."""
    script = BOOTSTRAP_DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert "http://dokploy:3000/api" in script
    assert '"compose.update"' in script
    assert '"autoDeploy": False' in script
    assert '"GIT_SHA"' in script
    assert "INFRA2_DOKPLOY_APP_NAME" in script
    assert "DOKPLOY_API_KEY is required in IaC Runner env or rendered secrets" in script
    assert "Selected Dokploy API base" in script
    assert "Dokploy compose before update" in script
    assert "Dokploy compose env before update" in script
    assert "Dokploy compose after update" in script
    assert "IaC Runner health attempt" in script


def test_iac_runner_bootstrap_deploy_script_uses_confirmed_dokploy_env() -> None:
    """Infra-011.8: token repair rebuilds with Dokploy's latest compose env."""
    script = BOOTSTRAP_DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert "INFRA2_CONFIRMED_ENV_B64=" in script
    assert "base64.b64encode(confirmed_env.encode()).decode()" in script
    assert 'base64 -d > "$env_file"' in script
    assert (
        "docker inspect iac-runner --format '{{range .Config.Env}}{{println .}}{{end}}' \\\n  > \"$env_file\""
        not in script
    )
    assert "Dokploy compose env is missing VAULT_ROLE_ID/VAULT_SECRET_ID" in script
    assert "Recovered DOKPLOY_API_KEY from current container env" in script
    assert "current container env are missing DOKPLOY_API_KEY" in script
    assert "if [ -f /secrets/.env ]; then" in script


def test_deploy_cache_reuses_only_when_requested_services_match(monkeypatch) -> None:
    """Verify that cached deployment results are only returned when requested services match."""
    monkeypatch.setenv("WEBHOOK_SECRET", "test-webhook-secret")
    sync_runner = _load_module(
        "sync_runner", IAC_RUNNER / "sync_runner.py", monkeypatch
    )
    fake_flask = types.ModuleType("flask")

    class FakeFlask:
        def __init__(self, _name):
            pass

        def route(self, *_args, **_kwargs):
            return lambda func: func

    fake_flask.Flask = FakeFlask
    fake_flask.jsonify = lambda payload: payload
    fake_flask.request = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "flask", fake_flask)
    webhook_server = _load_module(
        "webhook_server_under_test", IAC_RUNNER / "webhook_server.py", monkeypatch
    )

    # --- Scenario A: Cache Mismatch ---
    # Cache has ["platform/postgres"], request is for ["platform/redis"]
    webhook_server._recent_deploys.clear()
    key = webhook_server._deployment_key("staging", DEPLOY_SHA)
    cached_postgres = sync_runner.SyncResult(
        env="staging",
        ref=DEPLOY_SHA,
        requested_services=["platform/postgres"],
        results=[
            sync_runner.ServiceSyncResult(
                service="platform/postgres",
                task="postgres.sync",
                success=True,
                skipped=False,
            )
        ],
    )
    response_postgres = webhook_server._completed_response(
        "staging", DEPLOY_SHA, "ci", cached_postgres
    )
    webhook_server._recent_deploys[key] = (time.monotonic(), response_postgres)

    fake_flask.request.headers = _signed_headers(monkeypatch)
    fake_flask.request.data = b""
    fake_flask.request.json = {
        "env": "staging",
        "ref": DEPLOY_SHA,
        "wait": True,
        "triggered_by": "ci",
        "services": ["platform/redis"],
    }

    started = []

    def fake_sync_services_by_version(env, ref, triggered_by, services):
        started.append((env, ref, services))
        return sync_runner.SyncResult(
            env=env,
            ref=ref,
            requested_services=services or [],
            results=[
                sync_runner.ServiceSyncResult(
                    service="platform/redis",
                    task="redis.sync",
                    success=True,
                )
            ],
        )

    monkeypatch.setattr(
        sync_runner, "sync_services_by_version", fake_sync_services_by_version
    )

    body, status_code = webhook_server.version_deploy()
    assert "cached" not in body
    assert started == [("staging", DEPLOY_SHA, ["platform/redis"])]

    # --- Scenario B: Cache Match ---
    # Cache has ["platform/postgres"], request is for ["platform/postgres"]
    webhook_server._recent_deploys.clear()
    webhook_server._recent_deploys[key] = (time.monotonic(), response_postgres)

    fake_flask.request.headers = _signed_headers(monkeypatch)
    fake_flask.request.data = b""
    fake_flask.request.json = {
        "env": "staging",
        "ref": DEPLOY_SHA,
        "wait": True,
        "triggered_by": "ci",
        "services": ["platform/postgres"],
    }

    started.clear()
    body2, status_code2 = webhook_server.version_deploy()
    assert body2.get("cached") is True
    assert not started  # No new deployment triggered
