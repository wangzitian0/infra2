"""Tests for IaC Runner deploy result semantics."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import types
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
IAC_RUNNER = ROOT / "bootstrap/06.iac_runner"
DEPLOY_PLATFORM_WORKFLOW = ROOT / ".github/workflows/deploy-platform.yml"
BOOTSTRAP_DEPLOY_SCRIPT = ROOT / "scripts/deploy_iac_runner_bootstrap.sh"


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
        headers={},
        data=b"",
        json={"env": "staging", "ref": "main", "wait": True, "triggered_by": "ci"},
    )
    monkeypatch.setitem(sys.modules, "flask", fake_flask)
    webhook_server = _load_module(
        "webhook_server_under_test", IAC_RUNNER / "webhook_server.py", monkeypatch
    )

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

    body, status_code = webhook_server.version_deploy()

    assert status_code == 202
    assert body["status"] == "in_progress"
    assert body["status_url"] == "/deploy/status"
    assert body["wait"] is False
    assert started == [("staging", "main", "ci")]


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
        headers={},
        data=b"",
        json={"env": "staging", "ref": "main", "wait": False, "triggered_by": "ci"},
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
        ref="main",
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
    fake_request.json = {"env": "staging", "ref": "main", "triggered_by": "ci"}
    status_body, status_code = webhook_server.deployment_status()

    assert status_code == 200
    assert status_body["status"] == "completed"
    assert status_body["result"]["succeeded"] == 1
    assert status_body["result"]["failed"] == 0


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
            return types.SimpleNamespace(
                returncode=0, stdout="resolved-root\n", stderr=""
            )
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


def test_deploy_platform_triggers_on_iac_runner_bootstrap_changes() -> None:
    """Infra-011.8: runner source changes must enter the deploy workflow."""
    workflow = DEPLOY_PLATFORM_WORKFLOW.read_text(encoding="utf-8")

    assert '"bootstrap/06.iac_runner/**"' in workflow
    assert "Detect IaC Runner bootstrap changes" in workflow
    assert "Deploy IaC Runner bootstrap changes" in workflow
    assert "scripts/deploy_iac_runner_bootstrap.sh" in workflow
    assert "/compare/${before}...${after}" in workflow
    assert ".files[]?.filename" in workflow
    assert "Changed files from GitHub API" in workflow
    assert "files_url=$files_url" in workflow
    assert "$GITHUB_EVENT_PATH" not in workflow
    assert "git diff-tree" not in workflow


def test_iac_runner_all_services_include_alerting_bridge(monkeypatch) -> None:
    """Infra-011.8: alerting bridge must not be left outside GitOps deploys."""
    sync_runner = _load_module(
        "sync_runner_alerting_under_test", IAC_RUNNER / "sync_runner.py", monkeypatch
    )

    assert sync_runner.SERVICE_TASK_MAP["platform/alerting"] == "alerting.sync"
    assert "platform/alerting" in sync_runner.ALL_SERVICES


def test_iac_runner_service_map_matches_discovered_deployers(monkeypatch) -> None:
    """Infra-011.8: every deploy.py service must be wired into GitOps."""
    from libs.deployer import discover_services

    sync_runner = _load_module(
        "sync_runner_discovery_under_test", IAC_RUNNER / "sync_runner.py", monkeypatch
    )
    discovered = discover_services()

    for service, task in discovered.items():
        assert sync_runner.SERVICE_TASK_MAP.get(service) == task
        assert service in sync_runner.ALL_SERVICES


def test_iac_runner_bootstrap_self_update_runs_before_health_check() -> None:
    """Infra-011.8: Actions updates stale runner code before polling /deploy."""
    workflow = DEPLOY_PLATFORM_WORKFLOW.read_text(encoding="utf-8")

    self_update = workflow.index("Deploy IaC Runner bootstrap changes")
    health_check = workflow.index("Check IaC Runner health")
    trigger_deploy = workflow.index("Trigger IaC Runner")

    assert self_update < health_check < trigger_deploy
    assert "INFRA2_WATCHDOG_SSH_PRIVATE_KEY" in workflow
    assert "steps.runner-changes.outputs.changed == 'true'" in workflow


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

    assert "docker inspect iac-runner --format" in script
    assert (
        'git -C "$code_dir" checkout -f "$INFRA2_DEPLOY_SHA" -- bootstrap/06.iac_runner'
        in script
    )
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
    assert "DOKPLOY_API_KEY is required in rendered IaC Runner secrets" in script
    assert "Selected Dokploy API base" in script
    assert "Dokploy compose before update" in script
    assert "Dokploy compose env before update" in script
    assert "Dokploy compose after update" in script
    assert "IaC Runner health attempt" in script
