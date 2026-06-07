"""Tests for Vault app-token lifecycle ownership."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import types

import pytest

from libs.vault_tokens import (
    accessor_kv_path,
    display_name,
    mask_token,
    policy_name,
)


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


def test_policy_names_and_accessor_paths_are_env_scoped() -> None:
    """AC7.5.5: Vault app-token identity includes project, env, and service."""
    assert (
        policy_name("finance_report", "staging", "app") == "finance_report-staging-app"
    )
    assert (
        display_name("finance_report", "staging", "app") == "finance_report/staging/app"
    )
    assert (
        accessor_kv_path("finance_report", "staging", "app")
        == "secret/bootstrap/staging/vault_token_accessors/finance_report/app"
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


def test_setup_tokens_includes_alerting_bridge_target(monkeypatch) -> None:
    """Infra-007 alerting: vault token setup can target the alerting bridge."""
    tasks, _exit_cls = _load_vault_tasks(monkeypatch)
    targets = tasks._vault_token_targets(str(ROOT))

    assert any(
        target.project == "platform"
        and target.service == "alerting"
        and target.service_dir == "12.alerting"
        for target in targets
    )


def test_mask_token_never_returns_full_secret_by_default() -> None:
    """AC7.5.5: setup-token output is safe for logs unless explicitly requested."""
    token = "hvs.new-secret-token-value"
    masked = mask_token(token)

    assert masked != token
    assert "secret-token" not in masked


def test_setup_tokens_can_target_one_service_and_revokes_old_accessor(
    monkeypatch, capsys
) -> None:
    """AC7.5.5: token repair is targeted and revokes the previous tracked accessor."""
    tasks, _exit_cls = _load_vault_tasks(monkeypatch)
    fake = FakeContext()

    monkeypatch.setenv("VAULT_ROOT_TOKEN", "root-token")
    monkeypatch.setenv("DEPLOY_ENV", "staging")
    monkeypatch.delenv("VAULT_SHOW_TOKENS", raising=False)
    monkeypatch.setattr(
        tasks,
        "get_env",
        lambda: {"ENV": "staging", "INTERNAL_DOMAIN": "zitian.party"},
    )
    monkeypatch.setattr(
        tasks, "_configure_dokploy_token", lambda *_args, **_kwargs: True
    )

    tasks.setup_tokens.body(fake, project="finance_report", service="app")

    commands = "\n".join(fake.commands)
    assert "-period=168h" in commands
    assert "-policy=finance_report-staging-app" in commands
    assert "-display-name=finance_report/staging/app" in commands
    assert "finance_report-staging-postgres" not in commands
    assert "vault token revoke -accessor old-accessor" in commands
    assert (
        "vault kv put secret/bootstrap/staging/vault_token_accessors/finance_report/app"
        in commands
    )

    output = capsys.readouterr().out
    assert "hvs.new-secret-token-value" not in output
    assert "hvs.ne...alue" in output


def test_setup_tokens_does_not_revoke_old_accessor_when_dokploy_update_fails(
    monkeypatch,
) -> None:
    """AC7.5.5: failed Dokploy injection must not break the currently running token."""
    tasks, exit_cls = _load_vault_tasks(monkeypatch)
    fake = FakeContext()

    monkeypatch.setenv("VAULT_ROOT_TOKEN", "root-token")
    monkeypatch.setenv("DEPLOY_ENV", "staging")
    monkeypatch.setattr(
        tasks,
        "get_env",
        lambda: {"ENV": "staging", "INTERNAL_DOMAIN": "zitian.party"},
    )
    monkeypatch.setattr(
        tasks, "_configure_dokploy_token", lambda *_args, **_kwargs: False
    )

    with pytest.raises(exit_cls):
        tasks.setup_tokens.body(fake, project="finance_report", service="app")

    commands = "\n".join(fake.commands)
    assert "vault token revoke -accessor old-accessor" not in commands


def test_setup_tokens_revokes_existing_dokploy_token_when_accessor_is_not_tracked(
    monkeypatch,
) -> None:
    """AC7.5.5: first managed rotation can revoke the previous Dokploy token."""
    tasks, _exit_cls = _load_vault_tasks(monkeypatch)
    fake = FakeContext(tracked_accessor=None)

    monkeypatch.setenv("VAULT_ROOT_TOKEN", "root-token")
    monkeypatch.setenv("DEPLOY_ENV", "staging")
    monkeypatch.setattr(
        tasks,
        "get_env",
        lambda: {"ENV": "staging", "INTERNAL_DOMAIN": "zitian.party"},
    )
    monkeypatch.setattr(
        tasks,
        "_configure_dokploy_token",
        lambda *_args, **_kwargs: {
            "configured": True,
            "previous_token": "old-dokploy-token",
        },
    )

    tasks.setup_tokens.body(fake, project="finance_report", service="app")

    commands = "\n".join(fake.commands)
    assert "vault token revoke -accessor old-dokploy-accessor" in commands
    assert "old-dokploy-token" not in commands


def test_setup_tokens_rejects_unknown_project_or_service(monkeypatch) -> None:
    """AC7.5.5: targeted repair fails closed for bad selectors."""
    tasks, exit_cls = _load_vault_tasks(monkeypatch)
    fake = FakeContext()

    monkeypatch.setenv("VAULT_ROOT_TOKEN", "root-token")
    monkeypatch.setattr(
        tasks,
        "get_env",
        lambda: {"ENV": "staging", "INTERNAL_DOMAIN": "zitian.party"},
    )

    with pytest.raises(exit_cls):
        tasks.setup_tokens.body(fake, project="finance_report", service="missing")


def test_configure_dokploy_token_retries_redeploy_until_runtime_record(
    monkeypatch,
) -> None:
    """AC7.5.5: token injection is successful only after runtime apply proof."""
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

    result = tasks._configure_dokploy_token(
        FakeContext(),
        service="iac_runner",
        token="hvs.new-token",
        project="bootstrap",
    )

    assert result == {"configured": True, "previous_token": "hvs.old-token"}
    assert client.updated_env == {"VAULT_APP_TOKEN": "hvs.new-token"}
    assert client.deploy_calls == 1
    assert client.redeploy_calls == 1


def test_setup_tokens_does_not_track_accessor_when_dokploy_runtime_apply_fails(
    monkeypatch,
) -> None:
    """AC7.5.5: a Dokploy env update without runtime proof must fail closed."""
    tasks, exit_cls = _load_vault_tasks(monkeypatch)
    fake = FakeContext()
    client = FakeDokployTokenDeploy(
        [
            [{"deploymentId": "old", "status": "done"}],
            [{"deploymentId": "old", "status": "done"}],
            [{"deploymentId": "old", "status": "done"}],
            [{"deploymentId": "old", "status": "done"}],
        ]
    )
    _install_fake_dokploy(monkeypatch, client)

    monkeypatch.setenv("VAULT_ROOT_TOKEN", "root-token")
    monkeypatch.setenv("DEPLOY_ENV", "production")
    monkeypatch.setenv("DOKPLOY_DEPLOYMENT_RECORD_TIMEOUT_SECONDS", "0")
    monkeypatch.setattr(tasks.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        tasks,
        "get_env",
        lambda: {"ENV": "production", "INTERNAL_DOMAIN": "zitian.party"},
    )
    monkeypatch.setattr(
        "libs.common.get_env",
        lambda: {"ENV": "production", "INTERNAL_DOMAIN": "zitian.party"},
    )

    with pytest.raises(exit_cls):
        tasks.setup_tokens.body(fake, project="bootstrap", service="iac_runner")

    commands = "\n".join(fake.commands)
    assert (
        "vault kv put secret/bootstrap/production/vault_token_accessors/bootstrap/iac_runner"
        not in commands
    )
    assert "vault token revoke -accessor old-accessor" not in commands
    assert client.deploy_calls == 1
    assert client.redeploy_calls == 1
