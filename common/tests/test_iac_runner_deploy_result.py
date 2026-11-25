"""Tests for IaC Runner deploy result semantics."""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import time
import types
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
IAC_RUNNER = ROOT / "bootstrap/06.iac_runner"
DEPLOY_PLATFORM_WORKFLOW = ROOT / ".github/workflows/deploy.yml"
RECONCILE_WORKFLOW = ROOT / ".github/workflows/reconcile-iac-inputs.yml"
BOOTSTRAP_DEPLOY_SCRIPT = ROOT / "scripts/deploy_iac_runner_bootstrap.sh"
IAC_RUNNER_VAULT_POLICY = IAC_RUNNER / "vault-policy.hcl"
DEPLOY_SHA = "a" * 40
_nonce_counter = 0


def _patch_thread(monkeypatch, module, fake) -> None:
    """Replace `Thread` for `module` only. Patching `module.threading.Thread` changes the
    global `threading` module, so anyio's portal thread (Starlette TestClient) gets the fake
    and fails with `unexpected keyword argument 'daemon'`."""
    import threading as _threading
    import types as _types

    proxy = _types.ModuleType("threading")
    proxy.__dict__.update(vars(_threading))
    proxy.Thread = fake
    monkeypatch.setattr(module, "threading", proxy)


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


def _post_signed(
    webhook_server,
    path: str,
    payload: dict,
    monkeypatch,
    extra_headers: dict[str, str] | None = None,
) -> tuple[dict, int]:
    from starlette.testclient import TestClient

    raw = json.dumps(payload).encode("utf-8")
    headers = {
        **_signed_headers(monkeypatch, raw),
        "Content-Type": "application/json",
        **(extra_headers or {}),
    }
    response = TestClient(webhook_server.app).post(path, content=raw, headers=headers)
    return response.json(), response.status_code


def _get_signed(webhook_server, path: str, monkeypatch) -> tuple[dict, int]:
    from starlette.testclient import TestClient

    response = TestClient(webhook_server.app).get(
        path, headers=_signed_headers(monkeypatch)
    )
    return response.json(), response.status_code


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
    monkeypatch.setattr(
        sync_runner,
        "prepare_ref_env",
        lambda _repo: sync_runner.RefEnv(python="/ref-env/bin/python"),
    )

    def fake_run(task_name, _repo_path, deploy_env, *_ref, python=None):
        assert python == "/ref-env/bin/python"
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
                    "common.env.VaultSecrets.VaultSecretNotFoundError:\n"
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


@pytest.mark.parametrize(
    ("mod_suffix", "message", "expected_kind", "expected_action"),
    [
        (
            "dokploy_auth",
            "WARNING: vault token lookup: permission denied on capabilities-self\n"
            "alerting sync failed: No GitHub provider found",
            "dokploy_auth_failed",
            "DOKPLOY_API_KEY",
        ),
        (
            "transient_aborted",
            "app: secret supply failed: transient transport error talking to Vault, "
            "retried 1 time(s) without success: Connection aborted.",
            "transient_transport",
            "1x",
        ),
    ],
)
def test_sync_result_classifies_failure_diagnostics(
    monkeypatch,
    mod_suffix: str,
    message: str,
    expected_kind: str,
    expected_action: str,
) -> None:
    """Route Dokploy auth and transient transport failures to accurate diagnostics."""
    sync_runner = _load_module(
        f"sync_runner_{mod_suffix}_under_test",
        IAC_RUNNER / "sync_runner.py",
        monkeypatch,
    )

    diagnostic = sync_runner.diagnose_failure(message)

    assert diagnostic["error_kind"] == expected_kind
    assert expected_action in diagnostic["next_action"]


def test_sync_result_classifies_a_retry_exhausted_tls_blip_as_transient_transport(
    monkeypatch,
) -> None:
    """#810: truealpha/app v0.0.90 staging failed once on an SSL EOF that 28 minutes
    earlier's v0.0.89 never hit. common/secrets_supply.py's retrying_transport already
    retried it (#759's backoff) before raising; the diagnostic must name it
    transient_transport with the retry count, not fall through to unknown_invoke_failure."""
    sync_runner = _load_module(
        "sync_runner_transient_transport_under_test",
        IAC_RUNNER / "sync_runner.py",
        monkeypatch,
    )

    diagnostic = sync_runner.diagnose_failure(
        "app: secret supply failed: transient transport error talking to Vault, "
        "retried 2 time(s) without success: <urlopen error [SSL: "
        "UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol "
        "(_ssl.c:1016)>"
    )

    assert diagnostic["error_kind"] == "transient_transport"
    assert "UNEXPECTED_EOF" in diagnostic["summary"]
    assert "2x" in diagnostic["next_action"]
    assert "container" not in diagnostic["next_action"]  # no more "go read the logs"


@pytest.mark.parametrize(
    "rendered_cause",
    (
        "[Errno 104] Connection reset by peer",
        "[Errno 53] Software caused connection abort",
    ),
)
def test_sync_result_classifies_errno_typed_resets_as_transient_transport(
    monkeypatch, rendered_cause: str
) -> None:
    """The two cases `retrying_transport` catches by TYPE, not by marker text.

    `_is_transient_transport_error` retries a `ConnectionResetError` /
    `ConnectionAbortedError` on an isinstance check, but Python renders those as
    "Connection reset by peer" / "Software caused connection abort" -- which carry
    none of the literal markers ("ECONNRESET", "Connection aborted") this classifier
    used to scan for. So exactly the errors the retry layer handles arrived here
    unrecognised and fell through to `unknown_invoke_failure`, the outcome #810
    exists to eliminate. Keyed on the wrapper's own phrase instead, which every
    retry-exhausted failure carries regardless of the OS's wording.
    """
    sync_runner = _load_module(
        f"sync_runner_errno_reset_{abs(hash(rendered_cause))}_under_test",
        IAC_RUNNER / "sync_runner.py",
        monkeypatch,
    )

    diagnostic = sync_runner.diagnose_failure(
        "app: secret supply failed: transient transport error talking to Vault, "
        f"retried 2 time(s) without success: {rendered_cause}"
    )

    assert diagnostic["error_kind"] == "transient_transport"
    assert "2x" in diagnostic["next_action"]
    assert rendered_cause in diagnostic["summary"]


def test_transient_transport_next_action_reads_as_english_without_a_count(
    monkeypatch,
) -> None:
    """When no "retried N time(s)" phrase is present the count is unknown, and the
    old fallback rendered it as "(severalx)". Say "several times" instead; the "Nx"
    form is only used when there is an N."""
    sync_runner = _load_module(
        "sync_runner_transient_no_count_under_test",
        IAC_RUNNER / "sync_runner.py",
        monkeypatch,
    )

    diagnostic = sync_runner.diagnose_failure(
        "app: secret supply failed: transient transport error talking to Vault: "
        "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred"
    )

    assert diagnostic["error_kind"] == "transient_transport"
    assert "severalx" not in diagnostic["next_action"]
    assert "several times" in diagnostic["next_action"]


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
    monkeypatch.setenv("WEBHOOK_SECRET", "test-webhook-secret")
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

    body, status_code = _post_signed(
        webhook_server,
        "/deploy",
        {"env": "staging", "ref": DEPLOY_SHA, "wait": True, "triggered_by": "ci"},
        monkeypatch,
    )

    assert status_code == 500
    assert body["status"] == "failed"
    assert body["result"]["failed"] == 1
    assert "stderr" not in body["result"]["results"][0]
    assert "stdout" not in body["result"]["results"][0]


def test_deploy_wait_string_false_keeps_async_path(monkeypatch) -> None:
    """#182: string wait=false must not become truthy."""
    _load_module("sync_runner", IAC_RUNNER / "sync_runner.py", monkeypatch)
    monkeypatch.setenv("WEBHOOK_SECRET", "test-webhook-secret")
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

    _patch_thread(monkeypatch, webhook_server, FakeThread)

    body, status_code = _post_signed(
        webhook_server,
        "/deploy",
        {"env": "staging", "ref": DEPLOY_SHA, "wait": "false", "triggered_by": "ci"},
        monkeypatch,
    )

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
    monkeypatch.setenv("WEBHOOK_SECRET", "test-webhook-secret")
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

    _patch_thread(monkeypatch, webhook_server, ImmediateThread)

    body, status_code = _post_signed(
        webhook_server,
        "/deploy",
        {"env": "staging", "ref": DEPLOY_SHA, "wait": False, "triggered_by": "ci"},
        monkeypatch,
    )

    assert status_code == 202
    assert body["status"] == "in_progress"
    status_body, status_code = _post_signed(
        webhook_server,
        "/deploy/status",
        {"env": "staging", "ref": DEPLOY_SHA, "triggered_by": "ci"},
        monkeypatch,
    )

    assert status_code == 200
    assert status_body["status"] == "completed"
    assert status_body["result"]["succeeded"] == 1
    assert status_body["result"]["failed"] == 0
    assert "stdout" not in status_body["result"]["results"][0]
    assert "stderr" not in status_body["result"]["results"][0]

    rest_body, rest_code = _get_signed(
        webhook_server, f"/deploy/status/{body['deployment_id']}", monkeypatch
    )
    assert rest_code == 200
    assert rest_body["status"] == "completed"
    assert rest_body["deployment_id"] == body["deployment_id"]


def test_deploy_wait_rejects_invalid_literal(monkeypatch) -> None:
    monkeypatch.setenv("WEBHOOK_SECRET", "test-webhook-secret")
    webhook_server = _load_module(
        "webhook_server_invalid_bool_under_test",
        IAC_RUNNER / "webhook_server.py",
        monkeypatch,
    )

    body, status_code = _post_signed(
        webhook_server,
        "/deploy",
        {"env": "staging", "ref": DEPLOY_SHA, "wait": "sometimes"},
        monkeypatch,
    )

    assert status_code == 400
    assert body["error"] == "wait must be a boolean"


def test_deploy_rejects_mutable_ref(monkeypatch) -> None:
    """Infra-011.10: /deploy must only accept immutable commit SHAs."""
    monkeypatch.setenv("WEBHOOK_SECRET", "test-webhook-secret")
    webhook_server = _load_module(
        "webhook_server_mutable_ref_under_test",
        IAC_RUNNER / "webhook_server.py",
        monkeypatch,
    )

    body, status_code = _post_signed(
        webhook_server,
        "/deploy",
        {"env": "staging", "ref": "main", "wait": False},
        monkeypatch,
    )

    assert status_code == 400
    assert body["error"] == "Ref must be an exact 40-character commit SHA"


def test_health_reports_missing_runtime_dependency(monkeypatch, tmp_path) -> None:
    """Infra-011.7: /health fails before deploy when runner dependencies drift."""
    from starlette.testclient import TestClient

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

    res = TestClient(webhook_server.app).get("/health")
    body = res.json()

    assert res.status_code == 503
    assert body["status"] == "degraded"
    assert body["checks"]["python:PyYAML"] is False


def test_health_requires_runner_process_service_account_token(
    monkeypatch, tmp_path
) -> None:
    """Infra-011.2: rendered secrets are insufficient if the process env is stale."""
    from starlette.testclient import TestClient

    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
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

    res = TestClient(webhook_server.app).get("/health")
    body = res.json()

    assert res.status_code == 503
    assert body["checks"]["op_service_account_token"] is False


def _git_in_workspace(repo_dir: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test Runner",
            "-c",
            "user.email=runner@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo_dir,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.strip()


def _configure_healthy_runner(webhook_server, monkeypatch, tmp_path: Path) -> Path:
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("VAULT_APP_TOKEN=redacted\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    repo_dir = workspace / "infra2"
    repo_dir.mkdir(parents=True)
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "sa-token")
    monkeypatch.setenv("DOKPLOY_API_KEY", "dokploy-key")
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "test-webhook-secret")
    monkeypatch.setattr(webhook_server, "SECRETS_FILE", secrets_file)
    monkeypatch.setattr(webhook_server, "WORKSPACE", workspace)
    monkeypatch.setattr(webhook_server, "op_service_account_works", lambda: True)
    monkeypatch.setattr(
        webhook_server,
        "_dependency_checks",
        lambda: {"python:PyYAML": True, "binary:git": True, "binary:op": True},
    )
    return repo_dir


def test_health_reports_live_workspace_git_sha_over_stale_env(
    monkeypatch, tmp_path
) -> None:
    """/health reads the mounted workspace HEAD instead of stale container ENV GIT_SHA.

    When bootstrap skips image rebuild, ENV GIT_SHA stays pinned to the older build
    commit (e.g. 8fb0cfb) while sync_runner updates /workspace/infra2 to a newer SHA.

    ### Mutation evidence
    - Reverting `/health` `git_sha` to `os.environ.get("GIT_SHA", "unknown")` fails this
      test because `body["git_sha"]` returns `"8fb0cfb"` instead of `first_sha`/`second_sha`.
    - Caching workspace SHA at module import time fails the second assertion after the
      workspace advances from `first_sha` to `second_sha`.
    """
    from starlette.testclient import TestClient

    monkeypatch.setenv("GIT_SHA", "8fb0cfb")
    webhook_server = _load_module(
        "webhook_server_health_live_sha_under_test",
        IAC_RUNNER / "webhook_server.py",
        monkeypatch,
    )
    repo_dir = _configure_healthy_runner(webhook_server, monkeypatch, tmp_path)
    _git_in_workspace(repo_dir, "init")
    (repo_dir / "README.md").write_text("v1\n", encoding="utf-8")
    _git_in_workspace(repo_dir, "add", "README.md")
    _git_in_workspace(repo_dir, "commit", "-m", "v1")
    first_sha = _git_in_workspace(repo_dir, "rev-parse", "--short", "HEAD")

    client = TestClient(webhook_server.app)
    res1 = client.get("/health")
    assert res1.status_code == 200
    assert res1.json()["git_sha"] == first_sha
    assert res1.json()["git_sha"] != "8fb0cfb"

    # Simulate sync_runner advancing the mounted workspace without container rebuild.
    (repo_dir / "README.md").write_text("v2\n", encoding="utf-8")
    _git_in_workspace(repo_dir, "add", "README.md")
    _git_in_workspace(repo_dir, "commit", "-m", "v2")
    second_sha = _git_in_workspace(repo_dir, "rev-parse", "--short", "HEAD")
    assert second_sha != first_sha

    res2 = client.get("/health")
    assert res2.status_code == 200
    assert res2.json()["git_sha"] == second_sha


def test_health_git_sha_falls_back_to_env_when_workspace_git_fails(
    monkeypatch, tmp_path
) -> None:
    """/health falls back to GIT_SHA env (or 'unknown') when workspace git fails.

    ### Mutation evidence
    - Dropping the `returncode == 0` check or `(OSError, subprocess.SubprocessError)`
      guard raises/returns empty instead of falling back to `"8fb0cfb"` and `"unknown"`.
    """
    from starlette.testclient import TestClient

    monkeypatch.setenv("GIT_SHA", "8fb0cfb")
    webhook_server = _load_module(
        "webhook_server_health_sha_fallback_under_test",
        IAC_RUNNER / "webhook_server.py",
        monkeypatch,
    )
    _configure_healthy_runner(webhook_server, monkeypatch, tmp_path)
    client = TestClient(webhook_server.app)

    # Non-git workspace directory -> git exits non-zero -> falls back to GIT_SHA env.
    res_env = client.get("/health")
    assert res_env.status_code == 200
    assert res_env.json()["git_sha"] == "8fb0cfb"

    # Subprocess timeout with blank GIT_SHA -> bounded fallback to "unknown".
    monkeypatch.setenv("GIT_SHA", "   ")

    def timed_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=2)

    monkeypatch.setattr(webhook_server.subprocess, "run", timed_out)
    res_unknown = client.get("/health")
    assert res_unknown.status_code == 200
    assert res_unknown.json()["git_sha"] == "unknown"


def test_health_reports_installed_sdk_version(monkeypatch, tmp_path) -> None:
    """/health reports the infra2-sdk version that the image installed (#1822).

    ### Mutation evidence
    - Removing `sdk_version` from the `/health` body fails the `"sdk_version" in body` check.
    - Reading the version from `uv.lock` of the workspace instead of the installed package
      fails the equality with `importlib.metadata.version("infra2-sdk")`.
    - A `task_sdk` value that the reconcile does not know fails the `task_sdk` equality.
    """
    import importlib.metadata

    from starlette.testclient import TestClient

    webhook_server = _load_module(
        "webhook_server_health_sdk_version_under_test",
        IAC_RUNNER / "webhook_server.py",
        monkeypatch,
    )
    _configure_healthy_runner(webhook_server, monkeypatch, tmp_path)
    body = TestClient(webhook_server.app).get("/health").json()

    assert "sdk_version" in body
    assert body["sdk_version"] == importlib.metadata.version("infra2-sdk")
    # The reconcile check reads this value (common/deploy/reconcile_iac_inputs.py).
    from common.deploy.reconcile_iac_inputs import RUNNER_TASK_SDK_REF_LOCK

    assert body["task_sdk"] == RUNNER_TASK_SDK_REF_LOCK

    def absent(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError("infra2-sdk")

    monkeypatch.setattr(webhook_server.importlib.metadata, "version", absent)
    assert TestClient(webhook_server.app).get("/health").json()["sdk_version"] == (
        "unknown"
    )


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


@pytest.mark.parametrize(
    ("env_name", "expected_suffix"), [("staging", "-staging"), ("production", "")]
)
def test_run_invoke_task_materializes_deploy_isolation_env(
    monkeypatch, tmp_path, env_name: str, expected_suffix: str
) -> None:
    """#161: IaC Runner child tasks materialize DEPLOY_ENV and isolation suffixes."""
    sync_runner = _load_module(
        f"sync_runner_{env_name}_env_under_test",
        IAC_RUNNER / "sync_runner.py",
        monkeypatch,
    )
    captured = {}

    def fake_run(args, **kwargs):
        captured["kwargs"] = kwargs
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(sync_runner.subprocess, "run", fake_run)

    result = sync_runner.run_invoke_task("postgres.sync", tmp_path, env_name)

    assert result["success"] is True
    invoke_env = captured["kwargs"]["env"]
    assert invoke_env["DEPLOY_ENV"] == env_name
    assert invoke_env["ENV_SUFFIX"] == expected_suffix
    assert invoke_env["ENV_DOMAIN_SUFFIX"] == expected_suffix


def test_run_invoke_task_propagates_exact_deploy_ref(monkeypatch, tmp_path) -> None:
    """Infra-011.19: child deployers record the exact checked-out revision."""
    sync_runner = _load_module(
        "sync_runner_deploy_ref_under_test",
        IAC_RUNNER / "sync_runner.py",
        monkeypatch,
    )
    captured = {}

    def fake_run(args, **kwargs):
        captured["env"] = kwargs["env"]
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(sync_runner.subprocess, "run", fake_run)

    result = sync_runner.run_invoke_task(
        "postgres.sync", tmp_path, "staging", DEPLOY_SHA.upper()
    )

    assert result["success"] is True
    assert captured["env"]["IAC_DEPLOY_REF"] == DEPLOY_SHA
    assert f"IAC_DEPLOY_REF={DEPLOY_SHA}" in sync_runner.safe_invoke_env_summary(
        captured["env"]
    )


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
    assert (
        "VAULT_TOKEN=set" in summary
    )  # the AppRole token travels as VAULT_TOKEN (PR-E)
    assert "role-abc" not in summary
    assert "secret-xyz" not in summary


@pytest.mark.parametrize(
    ("env_name", "suffix"), [("staging", "-staging"), ("production", "")]
)
def test_run_invoke_task_child_env_resolves_through_get_env(
    monkeypatch, tmp_path, env_name: str, suffix: str
) -> None:
    """#1039: get_env() has no default, so an iac-runner child must carry its environment.

    The child gets exactly the variables run_invoke_task builds. With only those, the
    child's get_env() must name the deploy environment the webhook asked for.
    """
    from common.core import environ

    sync_runner = _load_module(
        f"sync_runner_get_env_{env_name}_under_test",
        IAC_RUNNER / "sync_runner.py",
        monkeypatch,
    )
    captured = {}

    def fake_run(args, **kwargs):
        captured["env"] = kwargs["env"]
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(sync_runner.subprocess, "run", fake_run)
    sync_runner.run_invoke_task("postgres.sync", tmp_path, env_name)

    class _NoOnePasswordRead:
        def get(self, key):
            return None

    monkeypatch.setattr(
        "common.security.store.OpSecrets", lambda *a, **k: _NoOnePasswordRead()
    )
    for key in ("INFRA_ENVIRONMENT", "DEPLOY_ENV", "ENV_SUFFIX", "ENV_DOMAIN_SUFFIX"):
        monkeypatch.delenv(key, raising=False)
        if key in captured["env"]:
            monkeypatch.setenv(key, captured["env"][key])
    environ.reset_env_cache()

    resolved = environ.get_env()
    assert resolved["ENV"] == env_name
    assert resolved["ENV_SUFFIX"] == suffix


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


def _approle_env(monkeypatch, *, with_addr: bool = True) -> None:
    monkeypatch.delenv("VAULT_ROOT_TOKEN", raising=False)
    monkeypatch.delenv("VAULT_APP_TOKEN", raising=False)
    monkeypatch.setenv("VAULT_ROLE_ID", "role-abc")
    monkeypatch.setenv("VAULT_SECRET_ID", "secret-xyz")
    if with_addr:
        monkeypatch.setenv("VAULT_ADDR", "https://vault.example")
    else:
        monkeypatch.delenv("VAULT_ADDR", raising=False)


@pytest.mark.parametrize(
    ("mod_suffix", "with_addr", "returncode", "stdout"),
    [
        ("login_fail", True, 2, ""),
        ("no_addr", False, 0, ""),
        ("bad_json", True, 0, "not-json"),
    ],
)
def test_run_invoke_task_approle_login_edge_cases_yield_no_root_token(
    monkeypatch,
    tmp_path,
    mod_suffix: str,
    with_addr: bool,
    returncode: int,
    stdout: str,
) -> None:
    """#369: failed/absent/unparseable AppRole login yields no VAULT_ROOT_TOKEN."""
    _approle_env(monkeypatch, with_addr=with_addr)
    sync_runner = _load_module(
        f"sync_runner_{mod_suffix}", IAC_RUNNER / "sync_runner.py", monkeypatch
    )
    captured = {}

    def fake_run(args, **kwargs):
        if args[:1] == ["vault"]:
            if not with_addr:
                raise AssertionError(
                    "must not attempt AppRole login without VAULT_ADDR"
                )
            return types.SimpleNamespace(
                returncode=returncode, stdout=stdout, stderr="Error"
            )
        captured["invoke_env"] = kwargs["env"]
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(sync_runner.subprocess, "run", fake_run)

    sync_runner.run_invoke_task("postgres.sync", tmp_path, "staging")
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
    """Infra-011.8 & #1416: deploy.yml has no on.push trigger; runner source changes
    enter the release train (reconcile-iac-inputs.yml) before tools.reconcile_iac_inputs."""
    deploy_spec = yaml.safe_load(DEPLOY_PLATFORM_WORKFLOW.read_text(encoding="utf-8"))
    deploy_triggers = deploy_spec.get("on", deploy_spec.get(True)) or {}
    assert "push" not in deploy_triggers
    assert "workflow_dispatch" in deploy_triggers

    reconcile_spec = yaml.safe_load(RECONCILE_WORKFLOW.read_text(encoding="utf-8"))
    reconcile_step = next(
        s
        for s in reconcile_spec["jobs"]["operation"]["steps"]
        if s.get("name") == "Operation phase"
    )["run"]
    assert "bootstrap/06.iac_runner/*" in reconcile_step
    assert "scripts/deploy_iac_runner_bootstrap.sh" in reconcile_step
    assert "Deploy IaC Runner bootstrap changes" in reconcile_step
    assert "Check IaC Runner health" in reconcile_step
    assert reconcile_step.index(
        "Deploy IaC Runner bootstrap changes"
    ) < reconcile_step.index('python -m tools.reconcile_iac_inputs "${args[@]}"')


def test_deploy_platform_no_longer_triggers_platform_service_deploys() -> None:
    """#370 cutover: deploy.yml updates ONLY the iac_runner bootstrap; platform
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
    assert '"common/**"' not in workflow
    assert '"finance/**"' not in workflow


def test_iac_runner_all_services_include_alerting_bridge(monkeypatch) -> None:
    """Infra-011.8: alerting bridge must not be left outside GitOps deploys."""
    sync_runner = _load_module(
        "sync_runner_alerting_under_test", IAC_RUNNER / "sync_runner.py", monkeypatch
    )

    assert sync_runner._service_task_map()["platform/alerting"] == "alerting.sync"
    assert "platform/alerting" in sync_runner._all_services()


def test_iac_runner_service_map_matches_discovered_deployers(monkeypatch) -> None:
    """Infra-011.8: every deploy.py service must be wired into GitOps."""
    from common.deploy.deployer import discover_services

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
    """Infra-011.8: self-update preserves Dokploy drift outside the runner source and
    the files its image bakes (uv.lock and tools/sdk_requirement.py; #1115)."""
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


_ALLOWED_CODE_DIR_GIT_VERBS = {"fetch", "checkout", "rev-parse"}
_SHELL_OPERATOR_TOKENS = {"||", "&&", "|", ";", "&"}


def _checked_out_paths(script: str) -> list[str]:
    """Paths of the one `git -C "$code_dir" checkout -f "$INFRA2_DEPLOY_SHA" -- <paths>`.

    shlex reads the shell quoting and drops a trailing comment. A comment line, or a
    line that is not a command, never counts. The script may not check out twice, run
    any git verb on `$code_dir` outside `_ALLOWED_CODE_DIR_GIT_VERBS`, `cd` into
    `$code_dir`, or append shell operators to the checkout line (#1155).
    """
    prefix = ["git", "-C", "$code_dir", "checkout", "-f", "$INFRA2_DEPLOY_SHA", "--"]
    commands: list[list[str]] = []
    for line in script.replace("\\\n", " ").splitlines():
        if line.lstrip().startswith("#"):
            continue
        try:
            words = shlex.split(line, comments=True)
        except ValueError:  # part of a heredoc or an unbalanced quote: not our command
            continue
        for index, word in enumerate(words):
            cleaned = word.lstrip("(").rstrip(");")
            if cleaned == "cd" and index + 1 < len(words):
                target = words[index + 1].rstrip(");")
                assert "code_dir" not in target and "compose_root" not in target, (
                    f"the script must not cd into the clone: {words}"
                )
            if cleaned == "git" and any(
                "code_dir" in w or "compose_root" in w for w in words[index + 1 :]
            ):
                assert words[index : index + 3] == [
                    "git",
                    "-C",
                    "$code_dir",
                ], (
                    f'git on the clone must use exact `git -C "$code_dir"` prefix: {words}'
                )
        if words[:3] == ["git", "-C", "$code_dir"]:
            commands.append(words)
    checkouts = [words for words in commands if words[3:4] == ["checkout"]]
    assert len(checkouts) == 1, f"expected one checkout in the clone: {checkouts}"
    disallowed = [
        words
        for words in commands
        if not words[3:4] or words[3] not in _ALLOWED_CODE_DIR_GIT_VERBS
    ]
    assert not disallowed, (
        f"the script runs disallowed git verb(s) on the clone: {disallowed}"
    )
    words = checkouts[0]
    assert words[: len(prefix)] == prefix, f"unexpected checkout command: {words}"
    paths = [word.rstrip("/") for word in words[len(prefix) :]]
    assert paths, "checkout command names no paths"
    for path in paths:
        assert (
            path not in _SHELL_OPERATOR_TOKENS
            and not any(op in path for op in _SHELL_OPERATOR_TOKENS)
            and not path.startswith("-")
        ), f"checkout line contains shell operator or flag in path list: {words}"
    return paths


def _copy_sources(dockerfile: str) -> list[str]:
    """COPY/ADD sources from the build context, as written (plain or JSON form)."""
    sources: list[str] = []
    for line in dockerfile.replace("\\\n", " ").splitlines():
        instruction, _, rest = line.strip().partition(" ")
        if instruction.upper() not in {"COPY", "ADD"} or "--from=" in rest:
            continue
        words = (
            [str(word) for word in json.loads(rest)]
            if rest.startswith("[")
            else [word for word in shlex.split(rest) if not word.startswith("--")]
        )
        sources += words[:-1]
    return sources


def _service_builds(compose: dict) -> list[tuple[str, dict]]:
    """(service name, build mapping) for every compose service that builds an image."""
    builds = []
    for name, service in (compose.get("services") or {}).items():
        build = service.get("build") if isinstance(service, dict) else None
        if isinstance(build, str):
            build = {"context": build}
        if build:
            builds.append((name, build))
    return builds


def test_the_bootstrap_checkout_covers_every_path_the_runner_image_copies() -> None:
    """The VPS builds from the Dokploy clone, and the checkout above never moves its
    HEAD. A COPY source outside the checked-out paths is read from the clone's old
    working tree. On 2026-10-07 that tree was at 2026-06-17, and its uv.lock had no
    infra2-sdk entry, so the build of the #1116 image would have failed (#1115).
    `docker compose build` builds every service that has a `build:`, so the test reads
    all of them."""
    from common.deploy.config_hash import _dockerfile_copy_sources

    compose = yaml.safe_load((IAC_RUNNER / "compose.yaml").read_text(encoding="utf-8"))
    builds = _service_builds(compose)
    assert builds, "the compose file builds no image: the scan reads nothing"
    checked = _checked_out_paths(BOOTSTRAP_DEPLOY_SCRIPT.read_text(encoding="utf-8"))
    for name, build in builds:
        context = (IAC_RUNNER / build["context"]).resolve()
        dockerfile_path = context / build.get("dockerfile", "Dockerfile")
        dockerfile = dockerfile_path.read_text(encoding="utf-8")
        # A bind mount reads context files that neither parser models: fail closed.
        assert "--mount" not in dockerfile, (
            f"{name}: RUN --mount reads files this test cannot see"
        )
        # The parser that the config hash uses. It reads the JSON form and skips --from.
        files = _dockerfile_copy_sources(dockerfile_path, context)
        assert files, f"{name}: no COPY source found: the scan reads nothing"
        for path in files:
            rel = path.relative_to(ROOT).as_posix()
            assert any(rel == item or rel.startswith(item + "/") for item in checked), (
                f"{rel} is COPYed by the {name} image but the bootstrap script does "
                f"not check it out at the deploy SHA (checked out: {checked})"
            )
        # `git checkout <sha> -- <dir>` leaves files that the SHA deleted. A directory
        # or a glob in a COPY would bake them in, so every source names one file.
        for source in _copy_sources(dockerfile):
            assert (context / source).is_file(), (
                f"{name}: COPY source {source!r} must name one file, not a directory "
                "or a glob"
            )


def _push_path_matches(rel: str, pattern: str) -> bool:
    """True when repository-relative `rel` matches a GitHub Actions `on.push.paths` pattern."""
    clean = pattern.rstrip("/")
    if clean.endswith("/**"):
        prefix = clean[:-3]
        return rel == prefix or rel.startswith(prefix + "/")
    return rel == clean


def test_deploy_workflow_push_paths_cover_every_runner_dockerfile_copy_source() -> None:
    """#1155 & #1416: deploy.yml has no on.push trigger, and every file the runner image
    COPYs matches the in-train bootstrap filter in reconcile-iac-inputs.yml so a change to
    `uv.lock` or `tools/sdk_requirement.py` updates the runner during release promotion."""
    import fnmatch
    from common.deploy.config_hash import _dockerfile_copy_sources

    spec = yaml.safe_load(DEPLOY_PLATFORM_WORKFLOW.read_text(encoding="utf-8"))
    triggers = spec.get("on", spec.get(True)) or {}
    assert "push" not in triggers, "deploy.yml must not declare on.push"

    reconcile_wf = yaml.safe_load(RECONCILE_WORKFLOW.read_text(encoding="utf-8"))
    reconcile_steps = reconcile_wf["jobs"]["operation"]["steps"]
    run_script = next(
        s["run"] for s in reconcile_steps if s.get("name") == "Operation phase"
    )
    non_comment_lines = [
        line for line in run_script.splitlines() if not line.lstrip().startswith("#")
    ]
    train_patterns = [
        pat.strip()
        for line in non_comment_lines
        if line.strip().endswith(")")
        and "|" in line
        and "bootstrap/06.iac_runner" in line
        for pat in line.strip()[:-1].split("|")
    ]
    expected_patterns = [
        "bootstrap/06.iac_runner/*",
        "scripts/deploy_iac_runner_bootstrap.sh",
        "uv.lock",
        "tools/sdk_requirement*.py",
    ]
    assert train_patterns == expected_patterns

    compose = yaml.safe_load((IAC_RUNNER / "compose.yaml").read_text(encoding="utf-8"))
    builds = _service_builds(compose)
    assert builds, "the compose file builds no image"
    for name, build in builds:
        context = (IAC_RUNNER / build["context"]).resolve()
        dockerfile_path = context / build.get("dockerfile", "Dockerfile")
        files = _dockerfile_copy_sources(dockerfile_path, context)
        assert files, f"{name}: no COPY source found"
        for path in files:
            rel = path.relative_to(ROOT).as_posix()
            assert any(
                fnmatch.fnmatch(rel, pat) or _push_path_matches(rel, pat)
                for pat in train_patterns
            ), (
                f"{rel} is COPYed by the {name} image but matches no bootstrap "
                f"pattern in reconcile-iac-inputs.yml ({train_patterns})"
            )


@pytest.mark.parametrize(
    "mutated_script",
    [
        # 1. Subshell cd + git reset after checkout
        BOOTSTRAP_DEPLOY_SCRIPT.read_text(encoding="utf-8")
        + '\n(cd "$code_dir" && git reset --hard HEAD)\n',
        # 2. Disallowed git verb `read-tree` on $code_dir
        BOOTSTRAP_DEPLOY_SCRIPT.read_text(encoding="utf-8")
        + '\ngit -C "$code_dir" read-tree --reset -u HEAD\n',
        # 3. Appending `|| true` to the checkout line
        BOOTSTRAP_DEPLOY_SCRIPT.read_text(encoding="utf-8").replace(
            'git -C "$code_dir" checkout -f "$INFRA2_DEPLOY_SHA" -- bootstrap/06.iac_runner uv.lock tools/sdk_requirement.py',
            'git -C "$code_dir" checkout -f "$INFRA2_DEPLOY_SHA" -- bootstrap/06.iac_runner uv.lock tools/sdk_requirement.py || true',
        ),
        # 4. Aliasing via --git-dir / --work-tree instead of `git -C "$code_dir"`
        BOOTSTRAP_DEPLOY_SCRIPT.read_text(encoding="utf-8")
        + '\ngit --git-dir="$code_dir/.git" --work-tree="$code_dir" checkout -f HEAD -- .\n',
    ],
    ids=[
        "subshell_cd_reset",
        "git_read_tree",
        "checkout_or_true",
        "git_dir_flag_alias",
    ],
)
def test_checked_out_paths_rejects_all_three_checkout_bypasses(
    mutated_script: str,
) -> None:
    """#1155: all 3 checkout test bypasses must fail `_checked_out_paths`."""
    with pytest.raises(AssertionError):
        _checked_out_paths(mutated_script)


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


def test_removed_github_webhook_and_openapi_contract(monkeypatch) -> None:
    """#1647 AC13/AC14: /webhook is removed; FastAPI /openapi.json, /livez, /readyz, and GET /deploy/status/{id} are active."""
    from starlette.testclient import TestClient

    monkeypatch.setenv("WEBHOOK_SECRET", "test-webhook-secret")
    webhook_server = _load_module(
        "webhook_server_openapi_contract",
        IAC_RUNNER / "webhook_server.py",
        monkeypatch,
    )
    client = TestClient(webhook_server.app)

    assert not hasattr(webhook_server, "run_sync")
    assert not hasattr(webhook_server, "webhook")
    assert client.post("/webhook", json={"ref": "refs/heads/main"}).status_code == 404

    assert client.get("/livez").status_code == 200
    assert client.get("/livez").json() == {"status": "alive", "service": "iac-runner"}

    # GET /deploy/status/{deployment_id} auth + validation + 404
    assert client.get(f"/deploy/status/{'a' * 16}").status_code == 401
    bad_id, bad_code = _get_signed(
        webhook_server, "/deploy/status/not-hex", monkeypatch
    )
    assert bad_code == 400
    assert "16 lowercase hex" in bad_id["error"]
    miss, miss_code = _get_signed(
        webhook_server, f"/deploy/status/{'a' * 16}", monkeypatch
    )
    assert miss_code == 404
    assert miss["status"] == "not_found"

    # OpenAPI contract
    openapi = client.get("/openapi.json")
    assert openapi.status_code == 200
    spec = openapi.json()
    paths = spec["paths"]
    assert "/webhook" not in paths
    for expected_path in (
        "/health",
        "/livez",
        "/readyz",
        "/deploy",
        "/deploy/status",
        "/deploy/status/{deployment_id}",
    ):
        assert expected_path in paths, expected_path
    assert (
        paths["/deploy"]["post"]["requestBody"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        == "#/components/schemas/DeployRequest"
    )
    assert (
        paths["/deploy/status"]["post"]["requestBody"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        == "#/components/schemas/DeployStatusRequest"
    )
    assert (
        paths["/deploy/status"]["post"]["responses"]["409"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        == "#/components/schemas/RunnerErrorResponse"
    )
    op_ids = [
        op["operationId"]
        for pi in paths.values()
        for op in pi.values()
        if isinstance(op, dict) and "operationId" in op
    ]
    assert len(op_ids) == len(set(op_ids))


@pytest.mark.asyncio
async def test_wait_deploy_does_not_block_event_loop_and_enforces_body_limit(
    monkeypatch,
) -> None:
    """PR #1746 audit fixes: blocking sync/wait runs in threadpool and >64 KiB bodies fail with 413."""
    import asyncio
    import threading
    import httpx

    sync_runner = _load_module(
        "sync_runner", IAC_RUNNER / "sync_runner.py", monkeypatch
    )
    monkeypatch.setenv("WEBHOOK_SECRET", "test-webhook-secret")
    webhook_server = _load_module(
        "webhook_server_async_loop_safety",
        IAC_RUNNER / "webhook_server.py",
        monkeypatch,
    )

    entered = threading.Event()
    release = threading.Event()

    def slow_sync(env, ref, *_args, **kwargs):
        entered.set()
        assert release.wait(timeout=5.0)
        return sync_runner.SyncResult(
            env=env,
            ref=ref,
            requested_services=["platform/redis"],
            results=[
                sync_runner.ServiceSyncResult(
                    service="platform/redis", task="redis.sync", success=True
                )
            ],
        )

    monkeypatch.setattr(sync_runner, "sync_services_by_version", slow_sync)

    raw_deploy = json.dumps(
        {
            "env": "staging",
            "ref": DEPLOY_SHA,
            "wait": True,
            "triggered_by": "ci",
            "services": ["platform/redis"],
        }
    ).encode("utf-8")
    deploy_headers = {
        **_signed_headers(monkeypatch, raw_deploy),
        "Content-Type": "application/json",
    }

    transport = httpx.ASGITransport(app=webhook_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        oversized = b"{" + b"a" * (webhook_server.MAX_REQUEST_BODY_BYTES + 10) + b"}"
        res_413 = await client.post("/deploy", content=oversized)
        assert res_413.status_code == 413

        res_413_status = await client.post("/deploy/status", content=oversized)
        assert res_413_status.status_code == 413

        deploy_task = asyncio.create_task(
            client.post("/deploy", content=raw_deploy, headers=deploy_headers)
        )
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()

        try:
            # While slow_sync is blocked inside wait=True /deploy, the asyncio event loop remains responsive
            live_resp = await asyncio.wait_for(client.get("/livez"), timeout=1.0)
            assert live_resp.status_code == 200
        finally:
            release.set()

        deploy_resp = await deploy_task
        assert deploy_resp.status_code == 200
        assert deploy_resp.json()["status"] == "completed"


def test_a_remembered_failure_is_retried_not_replayed(monkeypatch) -> None:
    """A retry after fixing the cause must actually redeploy (2026-09-09, v1.1.77).

    The staging soak failed on a stale local tag in the runner's workspace. The tag was
    repaired on the runner and the workflow was rerun; the rerun answered from the
    remembered failure — same deployment_id, same message — so the repair looked like it
    had changed nothing.
    """
    monkeypatch.setenv("WEBHOOK_SECRET", "test-webhook-secret")
    sync_runner = _load_module(
        "sync_runner", IAC_RUNNER / "sync_runner.py", monkeypatch
    )
    webhook_server = _load_module(
        "webhook_server_under_test", IAC_RUNNER / "webhook_server.py", monkeypatch
    )

    services = ["platform/alerting"]
    key = webhook_server._deployment_key("staging", DEPLOY_SHA, services)

    def _stored(success: bool) -> dict:
        return webhook_server._completed_response(
            "staging",
            DEPLOY_SHA,
            "deploy_v2",
            sync_runner.SyncResult(
                env="staging",
                ref=DEPLOY_SHA,
                requested_services=services,
                results=[
                    sync_runner.ServiceSyncResult(
                        service="platform/alerting",
                        task="alerting.sync",
                        success=success,
                        stderr="" if success else "would clobber existing tag",
                    )
                ],
            ),
            services,
        )

    started: list[tuple] = []

    def fake_sync(env, ref, *_args, **kwargs):
        started.append((env, ref, kwargs.get("services")))
        return sync_runner.SyncResult(
            env=env,
            ref=ref,
            requested_services=services,
            results=[
                sync_runner.ServiceSyncResult(
                    service="platform/alerting", task="alerting.sync", success=True
                )
            ],
        )

    monkeypatch.setattr(sync_runner, "sync_services_by_version", fake_sync)

    # A remembered FAILURE is not served: the request deploys again.
    webhook_server._recent_deploys.clear()
    webhook_server._recent_deploys[key] = (time.monotonic(), _stored(success=False))
    body, status_code = _post_signed(
        webhook_server,
        "/deploy",
        {
            "env": "staging",
            "ref": DEPLOY_SHA,
            "wait": True,
            "triggered_by": "deploy_v2",
            "services": services,
        },
        monkeypatch,
    )
    assert started, "a remembered failure must not stand in for a real retry"
    assert body.get("cached") is not True
    assert status_code == 200

    # A remembered SUCCESS still short-circuits — that is what the window is for.
    started.clear()
    webhook_server._recent_deploys.clear()
    webhook_server._recent_deploys[key] = (time.monotonic(), _stored(success=True))
    body2, status_code2 = _post_signed(
        webhook_server,
        "/deploy",
        {
            "env": "staging",
            "ref": DEPLOY_SHA,
            "wait": True,
            "triggered_by": "deploy_v2",
            "services": services,
        },
        monkeypatch,
    )
    assert not started
    assert body2.get("cached") is True
    assert status_code2 == 200

    # Polling the deployment's status still reports a failure — deploy_v2 depends on it,
    # and declining to reuse must not DISCARD it: the synchronous path reads the result
    # it stored after releasing the lock, so a concurrent request that popped the entry
    # here would make the original request answer "Deployment finished without a stored
    # result" about its own deploy (review on the PR that added _reusable_result).
    webhook_server._recent_deploys.clear()
    webhook_server._recent_deploys[key] = (time.monotonic(), _stored(success=False))
    assert webhook_server._reusable_result(key) is None
    assert webhook_server._recent_result(key)["status"] == "failed"
    assert key in webhook_server._recent_deploys


@pytest.mark.parametrize("action", ["sync", "secrets-supply"])
def test_a_retry_in_flight_is_reported_over_the_failure_it_retries(
    monkeypatch, action
) -> None:
    """truealpha v0.0.83 staging, 2026-09-17: the first attempt failed on a transient
    error (deployment_id 62d5bdb5bd8e4234); a retry with identical coordinates started a
    real run at 07:43:32Z that succeeded at 07:45:36Z, but deploy_v2 reported the retry
    failed after 1.2 s — its first /deploy/status poll was answered from the remembered
    failure, which shares the retry's deterministic deployment_id. While a key is in
    flight, any remembered result belongs to an earlier run."""
    monkeypatch.setenv("WEBHOOK_SECRET", "test-webhook-secret")
    sync_runner = _load_module(
        "sync_runner", IAC_RUNNER / "sync_runner.py", monkeypatch
    )
    webhook_server = _load_module(
        "webhook_server_retry_in_flight_under_test",
        IAC_RUNNER / "webhook_server.py",
        monkeypatch,
    )

    services = ["truealpha/data_engine"]
    version_ref = "v0.0.83"
    key = webhook_server._deployment_key(
        "staging", DEPLOY_SHA, services, version_ref, action
    )
    deployment_id = webhook_server._deployment_id(key)

    def _result(success: bool):
        return sync_runner.SyncResult(
            env="staging",
            ref=DEPLOY_SHA,
            requested_services=services,
            results=[
                sync_runner.ServiceSyncResult(
                    service="truealpha/data_engine",
                    task="data_engine.sync",
                    success=success,
                    stderr="" if success else "transient: registry timed out",
                )
            ],
        )

    # The first attempt's failure is remembered for the key.
    webhook_server._in_flight_deploys.clear()
    webhook_server._recent_deploys.clear()
    webhook_server._recent_deploys[key] = (
        time.monotonic(),
        webhook_server._completed_response(
            "staging",
            DEPLOY_SHA,
            "deploy_v2",
            _result(success=False),
            services,
            version_ref=version_ref,
            action=action,
        ),
    )

    # The retry starts and stays in flight until the test releases it.
    pending: list = []

    class DeferredThread:
        daemon = False

        def __init__(self, target=None, args=(), kwargs=None):
            self._target, self._args, self._kwargs = target, args, kwargs or {}

        def start(self):
            pending.append(self)

        def finish(self):
            self._target(*self._args, **self._kwargs)

    _patch_thread(monkeypatch, webhook_server, DeferredThread)
    monkeypatch.setattr(
        sync_runner,
        "sync_services_by_version",
        lambda *_args, **_kwargs: _result(success=True),
    )
    request_json = {
        "env": "staging",
        "ref": DEPLOY_SHA,
        "triggered_by": "deploy_v2",
        "services": services,
        "version_ref": version_ref,
        **({"action": action} if action != "sync" else {}),
    }
    body, status_code = _post_signed(
        webhook_server, "/deploy", {**request_json, "wait": False}, monkeypatch
    )
    assert status_code == 202
    assert body["status"] == "in_progress"
    assert body.get("cached") is not True
    assert body["deployment_id"] == deployment_id
    assert len(pending) == 1 and key in webhook_server._in_flight_deploys

    def _poll(payload: dict) -> tuple[dict, int]:
        return _post_signed(webhook_server, "/deploy/status", payload, monkeypatch)

    # Also verify RESTful GET /deploy/status/{deployment_id} sees in-flight before remembered failure
    rest_flight, rest_flight_code = _get_signed(
        webhook_server, f"/deploy/status/{deployment_id}", monkeypatch
    )
    assert rest_flight_code == 200
    assert rest_flight["status"] == "in_progress"

    # The retry's poll sees the retry, not the failure it is retrying — and the
    # in-progress answer carries the same id (and action) the poll asked about.
    status_body, status_code = _poll({**request_json, "deployment_id": deployment_id})
    assert status_code == 200, status_body
    assert status_body["status"] == "in_progress", status_body
    assert status_body["deployment_id"] == deployment_id
    assert status_body.get("action", "sync") == action
    assert status_body["version_ref"] == version_ref

    # An env/ref-only (legacy) poll finds one candidate, not an ambiguity, and it is
    # the run in flight as well.
    legacy_body, legacy_code = _poll(
        {"env": "staging", "ref": DEPLOY_SHA, "triggered_by": "deploy_v2"}
        | ({"action": action} if action != "sync" else {})
    )
    assert legacy_code == 200, legacy_body
    assert legacy_body["status"] == "in_progress", legacy_body
    assert legacy_body["deployment_id"] == deployment_id

    # Once the retry finishes, the same poll reports ITS result.
    pending.pop().finish()
    assert key not in webhook_server._in_flight_deploys
    status_body, status_code = _poll({**request_json, "deployment_id": deployment_id})
    assert status_code == 200, status_body
    assert status_body["status"] == "completed", status_body
    assert status_body["deployment_id"] == deployment_id
    assert status_body["result"]["succeeded"] == 1
    assert status_body["result"]["failed"] == 0


def test_deploy_cache_reuses_only_when_requested_services_match(monkeypatch) -> None:
    """Verify that cached deployment results are only returned when requested services match."""
    monkeypatch.setenv("WEBHOOK_SECRET", "test-webhook-secret")
    sync_runner = _load_module(
        "sync_runner", IAC_RUNNER / "sync_runner.py", monkeypatch
    )
    webhook_server = _load_module(
        "webhook_server_under_test", IAC_RUNNER / "webhook_server.py", monkeypatch
    )

    # --- Scenario A: Cache Mismatch ---
    # Cache has ["platform/postgres"], request is for ["platform/redis"]
    webhook_server._recent_deploys.clear()
    postgres_services = ["platform/postgres"]
    key = webhook_server._deployment_key("staging", DEPLOY_SHA, postgres_services)
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
        "staging", DEPLOY_SHA, "ci", cached_postgres, postgres_services
    )
    webhook_server._recent_deploys[key] = (time.monotonic(), response_postgres)

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

    body, status_code = _post_signed(
        webhook_server,
        "/deploy",
        {
            "env": "staging",
            "ref": DEPLOY_SHA,
            "wait": True,
            "triggered_by": "ci",
            "services": ["platform/redis"],
        },
        monkeypatch,
    )
    assert "cached" not in body
    assert started == [("staging", DEPLOY_SHA, ["platform/redis"])]

    # --- Scenario B: Cache Match ---
    # Cache has ["platform/postgres"], request is for ["platform/postgres"]
    webhook_server._recent_deploys.clear()
    webhook_server._recent_deploys[key] = (time.monotonic(), response_postgres)

    started.clear()
    body2, status_code2 = _post_signed(
        webhook_server,
        "/deploy",
        {
            "env": "staging",
            "ref": DEPLOY_SHA,
            "wait": True,
            "triggered_by": "ci",
            "services": ["platform/postgres"],
        },
        monkeypatch,
    )
    assert body2.get("cached") is True
    assert not started  # No new deployment triggered


def test_deployment_identity_includes_normalized_service_set(monkeypatch) -> None:
    """Infra-011.19: same env/ref with different services must be distinct operations."""
    monkeypatch.setenv("WEBHOOK_SECRET", "test-webhook-secret")
    webhook_server = _load_module(
        "webhook_server_identity_under_test",
        IAC_RUNNER / "webhook_server.py",
        monkeypatch,
    )

    postgres = webhook_server._deployment_key(
        "staging", DEPLOY_SHA, ["platform/postgres"]
    )
    redis = webhook_server._deployment_key("staging", DEPLOY_SHA, ["platform/redis"])
    batch_a = webhook_server._deployment_key(
        "staging", DEPLOY_SHA, ["platform/redis", "platform/postgres"]
    )
    batch_b = webhook_server._deployment_key(
        "staging", DEPLOY_SHA, [" platform/postgres ", "platform/redis"]
    )

    assert postgres != redis
    assert webhook_server._deployment_id(postgres) != webhook_server._deployment_id(
        redis
    )
    assert batch_a == batch_b
    assert webhook_server._deployment_id(batch_a) == webhook_server._deployment_id(
        batch_b
    )


def test_status_fails_closed_when_legacy_coordinate_is_ambiguous(monkeypatch) -> None:
    """Infra-011.19: old env/ref-only polling must never return another service result."""
    monkeypatch.setenv("WEBHOOK_SECRET", "test-webhook-secret")
    webhook_server = _load_module(
        "webhook_server_ambiguous_status_under_test",
        IAC_RUNNER / "webhook_server.py",
        monkeypatch,
    )
    postgres = webhook_server._deployment_key(
        "staging", DEPLOY_SHA, ["platform/postgres"]
    )
    redis = webhook_server._deployment_key("staging", DEPLOY_SHA, ["platform/redis"])
    webhook_server._in_flight_deploys.update({postgres, redis})

    body, status_code = _post_signed(
        webhook_server,
        "/deploy/status",
        {"env": "staging", "ref": DEPLOY_SHA, "triggered_by": "ci"},
        monkeypatch,
    )

    assert status_code == 409
    assert "Ambiguous deployment" in body["error"]
    assert set(body["candidate_deployment_ids"]) == {
        webhook_server._deployment_id(postgres),
        webhook_server._deployment_id(redis),
    }


def test_secrets_supply_action_is_its_own_deployment_and_reaches_the_sync(
    monkeypatch,
) -> None:
    """#649: deploy_v2 fires /deploy with action=secrets-supply before an app stack's
    Dokploy promote. The action must reach sync_services_by_version (which sets
    DEPLOY_ACTION for the child) and must key a different deployment than a plain sync
    of the same coordinates, or the 600 s request cache would answer one with the other."""
    sync_runner = _load_module(
        "sync_runner", IAC_RUNNER / "sync_runner.py", monkeypatch
    )
    monkeypatch.setenv("WEBHOOK_SECRET", "test-webhook-secret")
    webhook_server = _load_module(
        "webhook_server_action_under_test",
        IAC_RUNNER / "webhook_server.py",
        monkeypatch,
    )
    seen: dict = {}

    def fake_sync(env, ref, triggered_by, services=None, **kwargs):
        seen.update(env=env, services=services, **kwargs)
        return sync_runner.SyncResult(
            env=env,
            ref=ref,
            requested_services=list(services or []),
            results=[
                sync_runner.ServiceSyncResult(
                    service="finance_report/app", task="fr-app.sync", success=True
                )
            ],
        )

    monkeypatch.setattr(sync_runner, "sync_services_by_version", fake_sync)
    payload = {
        "env": "staging",
        "ref": DEPLOY_SHA,
        "wait": True,
        "triggered_by": "deploy_v2",
        "services": ["finance_report/app"],
        "action": "secrets-supply",
    }
    body, status_code = _post_signed(webhook_server, "/deploy", payload, monkeypatch)
    assert status_code == 200
    assert seen["action"] == "secrets-supply"
    assert body["action"] == "secrets-supply"
    plain = webhook_server._deployment_key(
        "staging", DEPLOY_SHA, ["finance_report/app"]
    )
    supply = webhook_server._deployment_key(
        "staging", DEPLOY_SHA, ["finance_report/app"], action="secrets-supply"
    )
    assert webhook_server._deployment_id(plain) != webhook_server._deployment_id(supply)
    assert body["deployment_id"] == webhook_server._deployment_id(supply)
    # a plain sync of the same coordinates is NOT served from the supply's cached result
    body2, _ = _post_signed(
        webhook_server, "/deploy", {**payload, "action": "sync"}, monkeypatch
    )
    assert body2.get("cached") is not True
    assert body2.get("action") != "secrets-supply"


def test_run_invoke_task_passes_the_action_to_the_child(monkeypatch, tmp_path) -> None:
    sync_runner = _load_module(
        "sync_runner_action_env_under_test", IAC_RUNNER / "sync_runner.py", monkeypatch
    )
    captured: dict = {}

    def fake_run(args, **kwargs):
        if args[:3] == ["vault", "write", "-format=json"]:
            return types.SimpleNamespace(
                returncode=0, stdout='{"auth": {"client_token": "t"}}', stderr=""
            )
        captured["env"] = kwargs["env"]
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(sync_runner.subprocess, "run", fake_run)
    sync_runner.run_invoke_task(
        "fr-app.sync", tmp_path, "staging", action="secrets-supply"
    )
    assert captured["env"]["DEPLOY_ACTION"] == "secrets-supply"
    sync_runner.run_invoke_task("fr-app.sync", tmp_path, "staging")
    assert "DEPLOY_ACTION" not in captured["env"]


def test_async_secrets_supply_response_id_matches_the_status_lookup(
    monkeypatch,
) -> None:
    """v1.1.69 incident: the 202 body for an async secrets-supply request carried the id of
    the plain sync (no action), so deploy_v2's poll of /deploy/status with the action got
    409 'deployment_id does not match'. Every response for the action must carry its id."""
    sync_runner = _load_module(
        "sync_runner", IAC_RUNNER / "sync_runner.py", monkeypatch
    )
    monkeypatch.setenv("WEBHOOK_SECRET", "test-webhook-secret")
    request_json = {
        "env": "staging",
        "ref": DEPLOY_SHA,
        "wait": False,
        "triggered_by": "deploy_v2",
        "services": ["finance_report/app"],
        "action": "secrets-supply",
    }
    webhook_server = _load_module(
        "webhook_server_async_action_under_test",
        IAC_RUNNER / "webhook_server.py",
        monkeypatch,
    )

    class ImmediateThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._target, self._args, self._kwargs = target, args, kwargs or {}

        def start(self):
            self._target(*self._args, **self._kwargs)

    _patch_thread(monkeypatch, webhook_server, ImmediateThread)

    def fake_sync(env, ref, triggered_by, services=None, **kwargs):
        return sync_runner.SyncResult(
            env=env, ref=ref, requested_services=list(services or []), results=[]
        )

    monkeypatch.setattr(sync_runner, "sync_services_by_version", fake_sync)
    body, status_code = _post_signed(
        webhook_server, "/deploy", dict(request_json), monkeypatch
    )
    expected = webhook_server._deployment_id(
        webhook_server._deployment_key(
            "staging", DEPLOY_SHA, ["finance_report/app"], action="secrets-supply"
        )
    )
    assert body["deployment_id"] == expected
    # and the status lookup with the same coordinates + action finds it (fresh
    # signature: nonces are single-use)
    status_body, status_code = _post_signed(
        webhook_server,
        "/deploy/status",
        {**request_json, "deployment_id": body["deployment_id"]},
        monkeypatch,
    )
    assert status_code == 200, status_body
    assert status_body["deployment_id"] == expected


def _mint_rs256_jwt(claims: dict, *, kid: str = "test-kid") -> tuple[str, dict]:
    import base64
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_numbers = private_key.public_key().public_numbers()

    def b64u(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    n_bytes = pub_numbers.n.to_bytes((pub_numbers.n.bit_length() + 7) // 8, "big")
    e_bytes = pub_numbers.e.to_bytes((pub_numbers.e.bit_length() + 7) // 8, "big")
    jwk = {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": b64u(n_bytes),
        "e": b64u(e_bytes),
    }
    header_b64 = b64u(json.dumps({"alg": "RS256", "typ": "JWT", "kid": kid}).encode())
    claims_b64 = b64u(json.dumps(claims).encode())
    signing_input = f"{header_b64}.{claims_b64}".encode("ascii")
    sig = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header_b64}.{claims_b64}.{b64u(sig)}", jwk


def test_production_deploy_requires_valid_github_oidc_token(monkeypatch) -> None:
    """#1126: /deploy with env=production rejects HMAC-only requests with 403 and accepts valid GitHub OIDC."""
    sync_runner = _load_module(
        "sync_runner", IAC_RUNNER / "sync_runner.py", monkeypatch
    )
    monkeypatch.setenv("WEBHOOK_SECRET", "test-webhook-secret")
    request_json = {
        "env": "production",
        "ref": DEPLOY_SHA,
        "wait": False,
        "triggered_by": "deploy_v2",
        "services": ["platform/redis"],
    }
    webhook_server = _load_module(
        "webhook_server_oidc_under_test",
        IAC_RUNNER / "webhook_server.py",
        monkeypatch,
    )

    # 1. Missing OIDC token -> 403
    body, status_code = _post_signed(
        webhook_server, "/deploy", dict(request_json), monkeypatch
    )
    assert status_code == 403
    assert "OIDC Bearer token" in body["error"]

    # 2. OIDC token for staging environment -> 403
    now_ts = int(time.time())
    bad_token, jwk = _mint_rs256_jwt(
        {
            "iss": "https://token.actions.githubusercontent.com",
            "aud": "iac-runner",
            "repository": "wangzitian0/infra2",
            "environment": "staging",
            "ref": "refs/tags/v1.2.3",
            "actor": "wangzitian0",
            "exp": now_ts + 300,
        }
    )
    monkeypatch.setattr(webhook_server, "_fetch_github_jwks", lambda: [jwk])
    body, status_code = _post_signed(
        webhook_server,
        "/deploy",
        dict(request_json),
        monkeypatch,
        extra_headers={"Authorization": f"Bearer {bad_token}"},
    )
    assert status_code == 403
    assert "environment must be 'production'" in body["error"]

    # 2b. OIDC token with wrong audience -> 403
    wrong_aud_token, jwk_aud = _mint_rs256_jwt(
        {
            "iss": "https://token.actions.githubusercontent.com",
            "aud": "https://github.com/wangzitian0",
            "repository": "wangzitian0/infra2",
            "environment": "production",
            "ref": "refs/tags/v1.2.3",
            "actor": "wangzitian0",
            "exp": now_ts + 300,
        }
    )
    monkeypatch.setattr(webhook_server, "_fetch_github_jwks", lambda: [jwk_aud])
    body, status_code = _post_signed(
        webhook_server,
        "/deploy",
        dict(request_json),
        monkeypatch,
        extra_headers={"Authorization": f"Bearer {wrong_aud_token}"},
    )
    assert status_code == 403
    assert "Unexpected OIDC audience" in body["error"]

    # 2c. OIDC token with wrong repository, expired exp, or non-tag/non-main ref -> 403
    for bad_claims, expected_err in (
        ({"repository": "evil/infra2"}, "Unexpected OIDC repository"),
        ({"exp": now_ts - 10}, "OIDC token has expired"),
        ({"ref": "refs/heads/feature/x"}, "OIDC token ref"),
    ):
        base_claims = {
            "iss": "https://token.actions.githubusercontent.com",
            "aud": "iac-runner",
            "repository": "wangzitian0/infra2",
            "environment": "production",
            "ref": "refs/tags/v1.2.3",
            "actor": "wangzitian0",
            "exp": now_ts + 300,
            **bad_claims,
        }
        tok, jwk_bad = _mint_rs256_jwt(base_claims)
        monkeypatch.setattr(webhook_server, "_fetch_github_jwks", lambda j=jwk_bad: [j])
        body, status_code = _post_signed(
            webhook_server,
            "/deploy",
            dict(request_json),
            monkeypatch,
            extra_headers={"Authorization": f"Bearer {tok}"},
        )
        assert status_code == 403
        assert expected_err in body["error"]

    # 3. Valid production OIDC token -> 202 and triggered_by bound to github-oidc:<actor>
    good_token, jwk2 = _mint_rs256_jwt(
        {
            "iss": "https://token.actions.githubusercontent.com",
            "aud": "iac-runner",
            "repository": "wangzitian0/infra2",
            "environment": "production",
            "ref": "refs/tags/v1.2.3",
            "actor": "wangzitian0",
            "exp": now_ts + 300,
        }
    )
    monkeypatch.setattr(webhook_server, "_fetch_github_jwks", lambda: [jwk2])

    class NoopThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            pass

        def start(self):
            pass

    _patch_thread(monkeypatch, webhook_server, NoopThread)
    monkeypatch.setattr(
        sync_runner,
        "sync_services_by_version",
        lambda env, ref, triggered_by, services=None, **kw: sync_runner.SyncResult(
            env=env, ref=ref, requested_services=list(services or []), results=[]
        ),
    )
    body, status_code = _post_signed(
        webhook_server,
        "/deploy",
        dict(request_json),
        monkeypatch,
        extra_headers={"Authorization": f"Bearer {good_token}"},
    )
    assert status_code == 202
    assert body["triggered_by"] == "github-oidc:wangzitian0"


def test_sync_runner_own_dir_services_matches_service_key_from_path(
    monkeypatch,
) -> None:
    """`sync_runner._own_dir_services` fallback must agree with `common.deploy.dependencies.service_key_from_path` across all service roots."""
    from common.deploy.dependencies import service_key_from_path

    sync_runner = _load_module(
        "sync_runner_own_dir_parity", IAC_RUNNER / "sync_runner.py", monkeypatch
    )
    sample_paths = [
        "platform/12.alerting/compose.yaml",
        "finance_report/finance_report/01.app/compose.yaml",
        "finance_report/finance_report/preview/compose.yaml",
        "truealpha/truealpha/01.app/compose.yaml",
        "truealpha/truealpha/preview/compose.yaml",
        "bootstrap/06.iac_runner/compose.yaml",
        "bootstrap/templates/vault-agent.compose.yaml",
        "common/deploy/config_hash.py",
        "tools/deploy_v2.py",
    ]
    for path in sample_paths:
        expected = {k for k in (service_key_from_path(path),) if k is not None}
        assert sync_runner._own_dir_services([path]) == expected, path


def test_diagnose_failure_classifies_dokploy_deployment_error_with_log_tail(
    monkeypatch,
) -> None:
    """#1366: diagnose_failure classifies Dokploy deployment record errors as
    dokploy_deployment_error and preserves deploymentId, logPath, and log tail."""
    sync_runner = _load_module(
        "sync_runner_dokploy_dep_error_under_test",
        IAC_RUNNER / "sync_runner.py",
        monkeypatch,
    )

    stderr = (
        "Traceback (most recent call last):\n"
        "RuntimeError: Dokploy deployment record entered error "
        "(deploymentId=dep-1366, logPath=/etc/dokploy/logs/truealpha/dep-1366.log, "
        "log_tail=Pulling app | Error: Head https://ghcr.io/v2/truealpha: 502 Bad Gateway)\n"
    )
    diagnostic = sync_runner.diagnose_failure(stderr)

    assert diagnostic["error_kind"] == "dokploy_deployment_error"
    assert "dep-1366" in diagnostic["summary"]
    assert "/etc/dokploy/logs/truealpha/dep-1366.log" in diagnostic["summary"]
    assert "502 Bad Gateway" in diagnostic["summary"]
    assert "/etc/dokploy/logs/truealpha/dep-1366.log" in diagnostic["fix_hint"]
    assert "/etc/dokploy/logs/truealpha/dep-1366.log" in diagnostic["next_action"]
