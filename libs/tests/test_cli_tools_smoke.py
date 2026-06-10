"""Smoke coverage for side-effect-light CLI helper modules."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from tools import dokploy_env, env_tool, local_init


class FakeSecrets:
    def __init__(self, values=None, set_result=True):
        self.values = dict(values or {})
        self.set_result = set_result
        self.set_calls = []

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.set_calls.append((key, value))
        return self.set_result

    def get_all(self):
        return self.values


class FakeDokployClient:
    def __init__(self):
        self.environments = []
        self.compose = None
        self.compose_env = ""
        self.latest_deployment = None
        self.compose_details = {}
        self.ensure_calls = []

    def list_environments(self, project):
        return self.environments

    def ensure_environment(self, project, env_name, description=""):
        self.ensure_calls.append((project, env_name, description))
        return {"environmentId": "env-1", "name": env_name}, True

    def find_compose_by_name(self, name, project_name="platform", env_name=None):
        return self.compose

    def get_compose_env(self, compose_id):
        return self.compose_env

    def get_latest_deployment(self, compose_id):
        return self.latest_deployment

    def get_compose(self, compose_id):
        return self.compose_details


def test_local_init_check_command_handles_success_and_errors(monkeypatch) -> None:
    monkeypatch.setattr(
        local_init.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )
    assert local_init._check_command("vault version") is True

    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("vault", timeout=10)

    monkeypatch.setattr(local_init.subprocess, "run", raise_timeout)
    assert local_init._check_command("vault version") is False


def test_local_init_collects_cli_status(monkeypatch) -> None:
    monkeypatch.setattr(local_init, "_check_command", lambda cmd: cmd.startswith("op"))

    results = local_init._collect_cli_status()

    assert ("op", True, "https://developer.1password.com/docs/cli") in results
    assert any(name == "vault" and ok is False for name, ok, _docs in results)


def test_env_tool_validates_type_values() -> None:
    assert env_tool._validate_type("bootstrap") == "bootstrap"
    assert env_tool._validate_type(None) is None
    assert env_tool._validate_type("invalid") is None


def test_env_tool_get_and_set_secret_use_selected_backend(monkeypatch) -> None:
    secrets = FakeSecrets({"TOKEN": "secret"})
    monkeypatch.setattr(env_tool, "get_secrets", lambda *args, **kwargs: secrets)

    env_tool.get.body(
        None,
        "TOKEN",
        project="platform",
        service="app",
        env="staging",
        credential_type="app_vars",
    )
    env_tool.set_secret.body(
        None,
        "TOKEN=new",
        project="platform",
        service="app",
        env="staging",
        credential_type="app_vars",
    )

    assert secrets.set_calls == [("TOKEN", "new")]


def test_env_tool_rejects_bad_set_format(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(env_tool, "get_secrets", lambda *args, **kwargs: calls.append(args))

    env_tool.set_secret.body(None, "TOKEN", credential_type="app_vars")

    assert calls == []


def test_dokploy_env_list_and_ensure_use_client(monkeypatch) -> None:
    client = FakeDokployClient()
    client.environments = [
        {"name": "production", "isDefault": True, "environmentId": "env-prod"}
    ]
    monkeypatch.setattr(dokploy_env, "get_dokploy", lambda host=None: client)

    dokploy_env.env_list.body(None, project="platform", host="cloud.example.test")
    dokploy_env.env_ensure.body(
        None,
        project="platform",
        env="Staging",
        description="staging env",
        host="cloud.example.test",
    )

    assert client.ensure_calls == [("platform", "staging", "staging env")]


def test_dokploy_logs_fails_fast_when_compose_or_host_missing(monkeypatch) -> None:
    client = FakeDokployClient()
    monkeypatch.setattr(dokploy_env, "get_dokploy", lambda host=None: client)
    monkeypatch.setattr(dokploy_env, "get_env", lambda: {})

    dokploy_env.logs.body(None, "app", project="platform")

    client.compose = {"composeId": "compose-1"}
    dokploy_env.logs.body(None, "app", project="platform")


def test_dokploy_logs_runs_deployment_tail(monkeypatch) -> None:
    client = FakeDokployClient()
    client.compose = {"composeId": "compose-1"}
    client.latest_deployment = {"deploymentId": "deploy-1", "logPath": "/tmp/deploy.log"}
    monkeypatch.setattr(dokploy_env, "get_dokploy", lambda host=None: client)
    monkeypatch.setattr(
        dokploy_env,
        "get_env",
        lambda: {"VPS_HOST": "vps.example.test", "INTERNAL_DOMAIN": "example.test"},
    )
    context = SimpleNamespace(commands=[])
    context.run = lambda cmd: context.commands.append(cmd)

    dokploy_env.logs.body(
        context, "app", project="platform", deployment=True, tail=25
    )

    assert context.commands == [
        "ssh root@vps.example.test 'tail -n 25 /tmp/deploy.log'"
    ]
