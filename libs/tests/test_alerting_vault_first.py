"""platform/alerting deploys on Vault when 1Password cannot answer (#625).

The 1Password service account behind OP_SERVICE_ACCOUNT_TOKEN was deleted; every
alerting sync failed on an `op` read, and because a platform reconcile is all-or-nothing,
every release tag since has failed to promote and the production marker has not moved.
The runtime secrets the sync used to copy from 1Password are already in Vault; the
deployer now reads them from there when 1Password answers nothing, and fails only when
Vault is empty too.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_deploy_module():
    spec = importlib.util.spec_from_file_location(
        "alerting_deploy_under_test", ROOT / "platform" / "12.alerting" / "deploy.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Secrets:
    def __init__(self, values):
        self.values = dict(values)
        self.writes: list[tuple[str, str]] = []

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.writes.append((key, value))
        self.values[key] = value
        return True


def _backends(monkeypatch, module, *, op: dict, vault: dict):
    backends = {"root_vars": _Secrets(op), "app_vars": _Secrets(vault)}

    def get_secrets(*, project, service, env, credential_type="app_vars", **_):
        return backends[credential_type]

    monkeypatch.setattr(module, "get_secrets", get_secrets)
    monkeypatch.setattr(
        module.AlertingDeployer, "env", classmethod(lambda cls: {"ENV": "staging"})
    )
    return backends


def test_sync_deploys_on_vault_when_1password_answers_nothing(
    monkeypatch, capsys
) -> None:
    module = _load_deploy_module()
    backends = _backends(
        monkeypatch,
        module,
        op={},
        vault={
            "ALERT_DELIVERY_MODE": "feishu_webhook",
            "FEISHU_WEBHOOK_URL": "https://open.feishu.cn/hook/x",
        },
    )
    assert module.AlertingDeployer._sync_1password_to_vault() is True
    assert backends["app_vars"].writes == []  # nothing to copy; Vault is the source
    assert "deploying on Vault (#625)" in capsys.readouterr().out


def test_sync_fails_only_when_vault_is_empty_too(monkeypatch) -> None:
    module = _load_deploy_module()
    _backends(monkeypatch, module, op={}, vault={})
    assert module.AlertingDeployer._sync_1password_to_vault() is False
    _backends(
        monkeypatch,
        module,
        op={},
        vault={"ALERT_DELIVERY_MODE": "feishu_app", "FEISHU_APP_ID": "id"},
    )
    assert module.AlertingDeployer._sync_1password_to_vault() is False


def test_sync_still_copies_from_1password_when_it_answers(monkeypatch) -> None:
    module = _load_deploy_module()
    backends = _backends(
        monkeypatch,
        module,
        op={
            "ALERT_DELIVERY_MODE": "feishu_webhook",
            "FEISHU_WEBHOOK_URL": "https://open.feishu.cn/hook/y",
        },
        vault={},
    )
    assert module.AlertingDeployer._sync_1password_to_vault() is True
    assert ("FEISHU_WEBHOOK_URL", "https://open.feishu.cn/hook/y") in backends[
        "app_vars"
    ].writes
