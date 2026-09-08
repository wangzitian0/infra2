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
        def from_environ(cls, env):
            seen.update(env)
            return cls()

    monkeypatch.setattr(secrets_supply, "UpdateOnlyVaultKv", FakeKv)
    secrets_supply.vault_backend(
        {"VAULT_ROOT_TOKEN": "legacy", "INTERNAL_DOMAIN": "x.io"}
    )
    assert seen["VAULT_TOKEN"] == "legacy"
    assert seen["VAULT_ADDR"] == "https://vault.x.io"
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

    backend = secrets_supply.UpdateOnlyVaultKv(
        "https://vault.test", token="t", transport=transport
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
