"""Tests for Vault app-token lifecycle ownership."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import types


from libs.vault_tokens import policy_name


ROOT = Path(__file__).resolve().parents[2]


def _install_task_stubs(monkeypatch):
    invoke_module = types.ModuleType("invoke")
    exceptions_module = types.ModuleType("invoke.exceptions")
    deployer_module = types.ModuleType("libs.deployer")
    console_module = types.ModuleType("libs.console")

    class Exit(Exception):
        def __init__(self, message="", code=1):
            super().__init__(message)
            self.message = message
            self.code = code

    def task(*args, **_kwargs):
        if args and callable(args[0]):
            return SimpleNamespace(body=args[0])
        return lambda fn: SimpleNamespace(body=fn)

    def emit(*args, **_kwargs):
        if args:
            print(" ".join(str(arg) for arg in args))

    invoke_module.task = task
    exceptions_module.Exit = Exit
    deployer_module.Deployer = object
    console_module.header = emit
    console_module.success = emit
    console_module.error = emit
    console_module.warning = emit
    console_module.info = emit
    console_module.prompt_action = emit
    console_module.run_with_status = lambda *_args, **_kwargs: None

    monkeypatch.setitem(sys.modules, "invoke", invoke_module)
    monkeypatch.setitem(sys.modules, "invoke.exceptions", exceptions_module)
    monkeypatch.setitem(sys.modules, "libs.deployer", deployer_module)
    monkeypatch.setitem(sys.modules, "libs.console", console_module)
    return Exit


def _load_vault_tasks(monkeypatch):
    exit_cls = _install_task_stubs(monkeypatch)
    path = ROOT / "bootstrap/05.vault/tasks.py"
    spec = importlib.util.spec_from_file_location("vault_tasks_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module, exit_cls


class FakeContext:
    def __init__(self, tracked_accessor: str | None = "old-accessor") -> None:
        self.commands: list[str] = []
        self.tracked_accessor = tracked_accessor

    def run(self, cmd, **kwargs):
        self.commands.append(cmd)
        env = kwargs.get("env") or {}
        if cmd == "vault token lookup -format=json":
            if env.get("VAULT_TOKEN") == "old-dokploy-token":
                return SimpleNamespace(
                    ok=True,
                    stdout=json.dumps({"data": {"accessor": "old-dokploy-accessor"}}),
                    stderr="",
                )
            return SimpleNamespace(
                ok=True, stdout=json.dumps({"data": {"ttl": 3600}}), stderr=""
            )
        if cmd.startswith("vault kv get"):
            if not self.tracked_accessor:
                return SimpleNamespace(ok=False, stdout="", stderr="not found")
            return SimpleNamespace(
                ok=True,
                stdout=json.dumps(
                    {"data": {"data": {"accessor": self.tracked_accessor}}}
                ),
                stderr="",
            )
        if cmd.startswith("vault token create"):
            return SimpleNamespace(
                ok=True,
                stdout=json.dumps(
                    {
                        "auth": {
                            "client_token": "hvs.new-secret-token-value",
                            "accessor": "new-accessor",
                        }
                    }
                ),
                stderr="",
            )
        return SimpleNamespace(ok=True, stdout="", stderr="")


class FakeDokployTokenDeploy:
    def __init__(self, deployment_snapshots: list[list[dict]]) -> None:
        self.compose_id = "compose-iac-runner"
        self.deployment_snapshots = list(deployment_snapshots)
        self.deploy_calls = 0
        self.redeploy_calls = 0
        self.updated_env: dict[str, str] = {}

    def find_compose_by_name(self, *_args, **_kwargs) -> dict:
        return {
            "composeId": self.compose_id,
            "env": "VAULT_APP_TOKEN=hvs.old-token\nENV=production\n",
        }

    def update_compose_env(self, compose_id, *, env_vars=None, env_str=None):
        assert compose_id == self.compose_id
        assert env_str is None
        self.updated_env.update(env_vars or {})
        return {"ok": True}

    def deploy_compose(self, compose_id):
        assert compose_id == self.compose_id
        self.deploy_calls += 1
        return {"ok": True}

    def redeploy_compose(self, compose_id):
        assert compose_id == self.compose_id
        self.redeploy_calls += 1
        return {"ok": True}

    def get_compose(self, compose_id):
        assert compose_id == self.compose_id
        if self.deployment_snapshots:
            return {"deployments": self.deployment_snapshots.pop(0)}
        return {"deployments": []}


def _install_fake_dokploy(monkeypatch, client: FakeDokployTokenDeploy) -> None:
    dokploy_module = types.ModuleType("libs.dokploy")
    dokploy_module.get_dokploy = lambda *_, **__: client
    monkeypatch.setitem(sys.modules, "libs.dokploy", dokploy_module)


def test_policy_names_are_env_scoped() -> None:
    """AC7.5.5: the per-service AppRole policy name includes project, env, and service."""
    assert (
        policy_name("finance_report", "staging", "app") == "finance_report-staging-app"
    )


def test_finance_report_vault_policies_are_env_scoped() -> None:
    """AC7.5.5: finance_report app tokens must not read every environment."""
    for policy_path in [
        ROOT / "finance_report/finance_report/01.postgres/vault-policy.hcl",
        ROOT / "finance_report/finance_report/02.redis/vault-policy.hcl",
        ROOT / "finance_report/finance_report/10.app/vault-policy.hcl",
    ]:
        policy = policy_path.read_text(encoding="utf-8")
        assert "/+/" not in policy, policy_path
        assert "{{env}}" in policy, policy_path


def test_vault_token_targets_includes_alerting_bridge(monkeypatch) -> None:
    """Infra-007 alerting: AppRole target discovery includes the alerting bridge."""
    tasks, _exit_cls = _load_vault_tasks(monkeypatch)
    targets = tasks._vault_token_targets(str(ROOT))

    assert any(
        target.project == "platform"
        and target.service == "alerting"
        and target.service_dir == "12.alerting"
        for target in targets
    )


def test_configure_dokploy_approle_injects_creds_and_redeploys(monkeypatch) -> None:
    """#257: AppRole injection (VAULT_ROLE_ID/VAULT_SECRET_ID) succeeds only after a runtime
    apply proof — same shared spine (`_redeploy_with_vault_creds`) and record-wait as the
    token path. This path previously had no direct unit test."""
    tasks, _exit_cls = _load_vault_tasks(monkeypatch)
    client = FakeDokployTokenDeploy(
        [
            [{"deploymentId": "old", "status": "done"}],
            [{"deploymentId": "old", "status": "done"}],
            [{"deploymentId": "old", "status": "done"}],
            [
                {"deploymentId": "old", "status": "done"},
                {"deploymentId": "new", "status": "done"},
            ],
        ]
    )
    _install_fake_dokploy(monkeypatch, client)
    monkeypatch.setattr(
        "libs.common.get_env",
        lambda: {"ENV": "production", "INTERNAL_DOMAIN": "zitian.party"},
    )
    monkeypatch.setenv("DOKPLOY_DEPLOYMENT_RECORD_TIMEOUT_SECONDS", "0")
    monkeypatch.setattr(tasks.time, "sleep", lambda _seconds: None)

    ok = tasks._configure_dokploy_approle(
        FakeContext(), "redis", "role-123", "secret-456", "platform"
    )

    assert ok is True
    assert client.updated_env == {
        "VAULT_ROLE_ID": "role-123",
        "VAULT_SECRET_ID": "secret-456",
    }
    assert client.deploy_calls == 1
    assert client.redeploy_calls == 1


def test_configure_dokploy_approle_returns_false_when_service_absent(monkeypatch) -> None:
    """#257: a service not registered in Dokploy fails closed (False), not a crash."""
    tasks, _exit_cls = _load_vault_tasks(monkeypatch)

    class _NoCompose:
        def find_compose_by_name(self, *_a, **_k):
            return None

    _install_fake_dokploy(monkeypatch, _NoCompose())
    monkeypatch.setattr(
        "libs.common.get_env",
        lambda: {"ENV": "production", "INTERNAL_DOMAIN": "zitian.party"},
    )

    ok = tasks._configure_dokploy_approle(
        FakeContext(), "ghost", "r", "s", "platform"
    )
    assert ok is False


def _policy_paths() -> set[str]:
    """Extract the `path "..."` globs granted by the IaC Runner Vault policy."""
    import re

    policy = (ROOT / "bootstrap" / "06.iac_runner" / "vault-policy.hcl").read_text(encoding="utf-8")
    return set(re.findall(r'path\s+"([^"]+)"', policy))


def _policy_matches(glob: str, path: str) -> bool:
    """Minimal Vault ACL match: `+` = exactly one segment, `*` = trailing segments."""
    g, p = glob.split("/"), path.split("/")
    for i, seg in enumerate(g):
        if seg == "*":
            return True
        if i >= len(p):
            return False
        if seg != "+" and seg != p[i]:
            return False
    return len(g) == len(p)


def test_policy_grants_metadata_list_for_service_secrets():
    """KV v2 LIST resolves to secret/metadata/, so a `list` on secret/data/ is a no-op;
    the policy must grant metadata for the service trees the runner enumerates."""
    paths = _policy_paths()
    for svc in (
        "secret/metadata/finance_report/production/app",
        "secret/metadata/platform/production/alerting",
    ):
        assert any(_policy_matches(g, svc) for g in paths), f"no metadata list grant for {svc}"
