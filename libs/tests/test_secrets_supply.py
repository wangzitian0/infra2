"""Deployer.apply_secret_supply: the pre_compose hook every registered service runs
(plan PR-E). The supply semantics themselves are covered by
test_alerting_vault_first.py; this file pins how a Deployer reacts to the report."""

from __future__ import annotations

import types

import pytest

import libs.deploy.deployer as deployer_module
from libs import secrets_supply
from libs.deploy.deployer import Deployer
from libs.secrets_supply import SupplyReport

ENV = {"ENV": "staging", "ENV_SUFFIX": "-staging", "VPS_HOST": "vps.test"}


class AlertingLike(Deployer):
    service = "alerting"
    project = "platform"
    compose_path = "platform/12.alerting/compose.yaml"
    secrets = (
        types.SimpleNamespace(
            vault_agent_container="platform-alerting-vault-agent${ENV_SUFFIX}",
            app_containers=("platform-alerting${ENV_SUFFIX}",),
        ),
    )


class Unregistered(Deployer):
    service = "nonesuch"
    project = "platform"
    compose_path = "platform/99.nonesuch/compose.yaml"


@pytest.fixture
def harness(monkeypatch):
    calls: dict = {"apply": [], "run": []}
    monkeypatch.setattr(AlertingLike, "env", classmethod(lambda cls: dict(ENV)))
    monkeypatch.setattr(Unregistered, "env", classmethod(lambda cls: dict(ENV)))
    monkeypatch.setattr(
        deployer_module,
        "run_with_status",
        lambda c, cmd, label: (
            calls["run"].append((cmd, label)) or calls.get("restart_ok", True)
        ),
    )

    def install(report: SupplyReport, *, changed_seen=()):
        def fake_apply(service, env, *, resolver=None, restart=None):
            calls["apply"].append((service.id, env))
            if restart is not None and changed_seen:
                restart(tuple(changed_seen))
            return report

        monkeypatch.setattr(secrets_supply, "apply", fake_apply)
        return calls

    return install


def test_changed_values_restart_the_agent_and_the_apps(harness) -> None:
    calls = harness(
        SupplyReport("platform/alerting", "staging", changed=("FEISHU_WEBHOOK_URL",)),
        changed_seen=("FEISHU_WEBHOOK_URL",),
    )
    assert AlertingLike.apply_secret_supply(object()) is True
    assert calls["apply"] == [("platform/alerting", "staging")]
    ((cmd, _label),) = calls["run"]
    assert cmd.startswith("ssh root@vps.test ")
    assert (
        "docker restart platform-alerting-vault-agent-staging platform-alerting-staging"
        in cmd
    )


def test_missing_required_values_stop_the_deploy(harness, capsys) -> None:
    calls = harness(
        SupplyReport(
            "platform/alerting",
            "staging",
            missing=("FEISHU_APP_ID",),
            notes=("1Password lacks ['FEISHU_APP_ID']",),
        )
    )
    assert AlertingLike.apply_secret_supply(object()) is False
    assert calls["run"] == []
    out = capsys.readouterr().out
    assert "FEISHU_APP_ID" in out


def test_unchanged_values_do_not_restart(harness) -> None:
    calls = harness(SupplyReport("platform/alerting", "staging"))
    assert AlertingLike.apply_secret_supply(object()) is True
    assert calls["run"] == []


def test_services_without_a_manifest_are_untouched(harness) -> None:
    calls = harness(SupplyReport("platform/nonesuch", "staging"))
    assert Unregistered.apply_secret_supply(object()) is True
    assert calls["apply"] == []


def test_supply_exceptions_fail_the_deploy_instead_of_crashing(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(AlertingLike, "env", classmethod(lambda cls: dict(ENV)))

    def boom(service, env, *, resolver=None, restart=None):
        raise RuntimeError("vault sealed")

    monkeypatch.setattr(secrets_supply, "apply", boom)
    assert AlertingLike.apply_secret_supply(object()) is False
    assert "vault sealed" in capsys.readouterr().out


def test_vault_backend_accepts_the_transition_alias(monkeypatch) -> None:
    seen: dict = {}

    class FakeKv:
        @classmethod
        def from_environ(cls, env, write_mode="patch"):
            seen.update(env)
            seen["write_mode"] = write_mode
            return cls()

    monkeypatch.setattr(secrets_supply, "VaultKvBackend", FakeKv)
    secrets_supply.vault_backend(
        {"VAULT_ROOT_TOKEN": "legacy", "INTERNAL_DOMAIN": "x.io"}
    )
    assert seen["VAULT_TOKEN"] == "legacy"
    assert seen["VAULT_ADDR"] == "https://vault.x.io"
    assert seen["write_mode"] == "update"  # the policies grant update, not patch
    seen.clear()
    secrets_supply.vault_backend(
        {"VAULT_TOKEN": "new", "VAULT_ROOT_TOKEN": "legacy", "VAULT_ADDR": "https://v"}
    )
    assert seen["VAULT_TOKEN"] == "new" and seen["VAULT_ADDR"] == "https://v"


def test_a_failed_restart_fails_the_deploy_closed(harness, capsys) -> None:
    calls = harness(
        SupplyReport("platform/alerting", "staging", changed=("FEISHU_WEBHOOK_URL",)),
        changed_seen=("FEISHU_WEBHOOK_URL",),
    )
    calls["restart_ok"] = False  # ssh/docker restart returned non-zero
    assert AlertingLike.apply_secret_supply(object()) is False
    assert len(calls["run"]) == 1
    assert "could not restart" in capsys.readouterr().out


def test_vault_writes_use_update_not_patch(monkeypatch) -> None:
    """Deploy identities have create/read/update/list, not patch (#649: runner got 403)."""
    import json as _json
    import types as _types

    from infra2_sdk.secrets import WriteResult

    calls: list[tuple[str, str, bytes | None]] = []

    def transport(method, url, headers, body):
        calls.append((method, url, body))
        if method == "GET":
            payload = {"data": {"data": {"A": "1", "B": "2"}}}
            return _types.SimpleNamespace(
                status=200, body=_json.dumps(payload).encode(), headers={}
            )
        return _types.SimpleNamespace(status=200, body=b"{}", headers={})

    from infra2_sdk.secrets import VaultKvBackend

    backend = VaultKvBackend(
        "https://vault.test", token="t", transport=transport, write_mode="update"
    )
    result = backend.write(
        "truealpha/staging/data_engine", {"B": "2", "SEC_USER_AGENT": "x"}
    )
    assert isinstance(result, WriteResult) and result.changed == ("SEC_USER_AGENT",)
    methods = [m for m, _u, _b in calls]
    assert "PATCH" not in methods and methods[-1] == "POST"
    sent = _json.loads(calls[-1][2])["data"]
    assert sent == {
        "A": "1",
        "B": "2",
        "SEC_USER_AGENT": "x",
    }  # merged document, nothing dropped
    assert (
        backend.write("truealpha/staging/data_engine", {"A": "1"}).changed == ()
    )  # no-op write


def test_workflow_deploy_jobs_install_the_pinned_sdk() -> None:
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for wf in ("deploy.yml", "deploy-report-main.yml", "preview-teardown.yml"):
        text = (root / ".github/workflows" / wf).read_text(encoding="utf-8")
        assert re.search(
            r"pip install invoke httpx python-dotenv rich .*infra2-sdk @ https://", text
        ), wf


def test_deployer_sync_under_secrets_supply_action_stops_after_the_supply(
    monkeypatch,
) -> None:
    """The runner's child gets DEPLOY_ACTION=secrets-supply from deploy_v2 (#649): only the
    supply runs; nothing is composed, so an app stack's Dokploy promote stays the deploy."""
    monkeypatch.setattr(AlertingLike, "env", classmethod(lambda cls: dict(ENV)))
    monkeypatch.setattr(deployer_module, "validate_env", lambda: [])
    monkeypatch.setenv("DEPLOY_ACTION", "secrets-supply")
    seen: list = []
    monkeypatch.setattr(
        AlertingLike,
        "apply_secret_supply",
        classmethod(lambda cls, c, env=None: seen.append(env) or True),
    )
    monkeypatch.setattr(
        AlertingLike,
        "verify_vault_app_token",
        classmethod(
            lambda cls: (_ for _ in ()).throw(
                AssertionError("must not reach the compose path")
            )
        ),
    )
    assert AlertingLike.sync(object()) == {
        "action": "supplied",
        "details": "secret supply applied; no compose",
    }
    assert seen == ["staging"]
    monkeypatch.setattr(
        AlertingLike, "apply_secret_supply", classmethod(lambda cls, c, env=None: False)
    )
    assert AlertingLike.sync(object())["action"] == "failed"


def test_deployer_sync_runs_the_supply_even_when_the_compose_hash_says_skip(
    monkeypatch,
) -> None:
    """#649: a human value changed in 1Password is a deploy where nothing else changed;
    the supply must not hide behind the compose change detection."""
    prod = {
        "ENV": "production",
        "INTERNAL_DOMAIN": "x.io",
        "DATA_PATH": "/data/platform/alerting",
        "VPS_HOST": "vps",
    }
    monkeypatch.setattr(AlertingLike, "env", classmethod(lambda cls: dict(prod)))
    monkeypatch.setattr(deployer_module, "validate_env", lambda: [])
    monkeypatch.delenv("DEPLOY_ACTION", raising=False)
    monkeypatch.setenv("IAC_DEPLOY_REF", "a" * 40)
    monkeypatch.setattr(
        AlertingLike,
        "verify_vault_app_token",
        classmethod(lambda cls: {"valid": True, "ttl_hours": 999}),
    )
    monkeypatch.setattr(
        AlertingLike, "ensure_runtime_secrets", classmethod(lambda cls, c: True)
    )
    monkeypatch.setattr(
        AlertingLike,
        "compose_env_base",
        classmethod(lambda cls, e=None: {"ENV": "production"}),
    )
    monkeypatch.setattr(
        AlertingLike,
        "source_config_env_base",
        classmethod(lambda cls, e=None: {"ENV": "production"}),
    )
    monkeypatch.setattr(
        AlertingLike,
        "config_env_with_vault_addr",
        classmethod(lambda cls, env, e: dict(env)),
    )
    monkeypatch.setattr(
        AlertingLike,
        "compute_local_config_hash",
        classmethod(lambda cls, c, env: "same-hash"),
    )
    supplied: list = []
    monkeypatch.setattr(
        AlertingLike,
        "apply_secret_supply",
        classmethod(lambda cls, c, env=None: supplied.append(env) or True),
    )

    def remote_identity():
        # whatever the remote-hash reader returns, the supply already ran; make the
        # rest of sync stop here deterministically
        raise RuntimeError("remote unreadable in this unit test")

    monkeypatch.setattr(
        AlertingLike,
        "get_remote_config_identity",
        classmethod(lambda cls: remote_identity()),
    )
    result = AlertingLike.sync(object())
    assert supplied == ["production"]
    assert (
        result["action"] == "skipped"
    )  # fail-closed skip path, reached AFTER the supply
