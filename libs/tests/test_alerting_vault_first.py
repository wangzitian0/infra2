"""The secret supply deploys on Vault when 1Password cannot answer (#625).

The 1Password service account behind OP_SERVICE_ACCOUNT_TOKEN was deleted once; every
alerting sync failed on an ``op`` read, and because a platform reconcile is
all-or-nothing, no release promoted for days. The manifest-driven supply that replaced
the bespoke alerting sync (plan PR-E, libs/secrets_supply.py) keeps the rule: when
1Password is unreachable the deploy proceeds on what Vault already holds and only a
Vault that lacks a required value fails it. These tests run the real infra2-sdk resolver
over in-memory stores and a small manifest with one field of each source class.
"""

from __future__ import annotations

from infra2_sdk.runtime.config_schema import EnvironmentManifest
from infra2_sdk.secrets import (
    SecretsError,
    SecretsResolver,
    WriteResult,
    op_item,
    vault_path,
)

from libs import secrets_supply
from libs.secrets_registry import Service

MANIFEST = EnvironmentManifest.from_dict(
    {
        "contract_version": 2,
        "source": "test/alerting",
        "fields": [
            {
                "field": "alert_delivery_mode",
                "env": "ALERT_DELIVERY_MODE",
                "source": "human",
                "injected": True,
            },
            {
                "field": "feishu_webhook_url",
                "env": "FEISHU_WEBHOOK_URL",
                "source": "human",
                "empty_ok": True,
                "sensitive": True,
            },
            {
                "field": "bridge_token",
                "env": "BRIDGE_TOKEN",
                "source": "runtime",
                "sensitive": True,
            },
            {
                "field": "heartbeat_token",
                "env": "INFRA_PROBE_HEARTBEAT_TOKEN",
                "source": "runtime",
                "sensitive": True,
                "mirror_to_1password": True,
            },
        ],
    }
)
SERVICE = Service("platform/12.alerting", "platform", "alerting", ("unused",))
STORE_PATH = vault_path("platform", "staging", "alerting")
HUMAN_ITEM = op_item("platform", "staging", "alerting")


class MemoryBackend:
    def __init__(self, items=None, *, fail: str | None = None):
        self.items = {k: dict(v) for k, v in (items or {}).items()}
        self.fail = fail

    def read(self, path):
        if self.fail:
            raise SecretsError(self.fail)
        return dict(self.items.get(path, {}))

    def write(self, path, values):
        if self.fail:
            raise SecretsError(self.fail)
        current = self.items.setdefault(path, {})
        changed = tuple(k for k, v in values.items() if current.get(k) != v)
        current.update(values)
        return WriteResult(changed=changed)


def _apply(*, store: MemoryBackend, human: MemoryBackend, restarts: list):
    resolver = SecretsResolver(
        MANIFEST,
        project="platform",
        service="alerting",
        env="staging",
        store=store,
        human=human,
    )
    return secrets_supply.apply(
        SERVICE,
        "staging",
        resolver=resolver,
        restart=lambda keys: restarts.append(keys),
    )


def test_deploys_on_vault_when_1password_cannot_be_reached() -> None:
    store = MemoryBackend(
        {
            STORE_PATH: {
                "ALERT_DELIVERY_MODE": "feishu_webhook",
                "FEISHU_WEBHOOK_URL": "https://open.feishu.cn/hook/x",
                "BRIDGE_TOKEN": "b",
                "INFRA_PROBE_HEARTBEAT_TOKEN": "h",
            }
        }
    )
    restarts: list = []
    report = _apply(
        store=store, human=MemoryBackend(fail="op: not signed in"), restarts=restarts
    )

    assert report.ok
    assert report.changed == ()  # nothing to copy; Vault is the source
    assert restarts == []  # unchanged values never restart the consumers
    assert any("deploying on Vault (#625)" in note for note in report.notes)


def test_fails_only_when_vault_lacks_a_required_value_too() -> None:
    store = MemoryBackend()
    restarts: list = []
    report = _apply(
        store=store, human=MemoryBackend(fail="op: not signed in"), restarts=restarts
    )

    assert not report.ok
    assert "ALERT_DELIVERY_MODE" in report.missing  # human, required, nowhere
    assert "FEISHU_WEBHOOK_URL" not in report.missing  # empty_ok is not a blocker
    # runtime values are still generated so the next deploy has them
    assert set(report.changed) == {"BRIDGE_TOKEN", "INFRA_PROBE_HEARTBEAT_TOKEN"}
    assert store.items[STORE_PATH].keys() == {
        "BRIDGE_TOKEN",
        "INFRA_PROBE_HEARTBEAT_TOKEN",
    }


def test_copies_human_values_mirrors_runtime_ones_and_restarts_once() -> None:
    human = MemoryBackend(
        {
            HUMAN_ITEM: {
                "ALERT_DELIVERY_MODE": "feishu_webhook",
                "FEISHU_WEBHOOK_URL": "https://open.feishu.cn/hook/y",
            }
        }
    )
    store = MemoryBackend()
    restarts: list = []
    report = _apply(store=store, human=human, restarts=restarts)

    assert report.ok, report.summary()
    held = store.items[STORE_PATH]
    assert held["ALERT_DELIVERY_MODE"] == "feishu_webhook"
    assert held["FEISHU_WEBHOOK_URL"] == "https://open.feishu.cn/hook/y"
    assert held["BRIDGE_TOKEN"] and held["INFRA_PROBE_HEARTBEAT_TOKEN"]
    # the token humans need to paste into the heartbeat worker is mirrored back
    assert (
        human.items[HUMAN_ITEM]["INFRA_PROBE_HEARTBEAT_TOKEN"]
        == held["INFRA_PROBE_HEARTBEAT_TOKEN"]
    )
    assert (
        "BRIDGE_TOKEN" not in human.items[HUMAN_ITEM]
    )  # not flagged, stays in Vault only
    assert restarts == [report.changed]
    assert "FEISHU_WEBHOOK_URL" in report.changed

    # a second deploy with nothing new is a no-op: no writes, no restart
    again = _apply(store=store, human=human, restarts=restarts)
    assert again.ok and again.changed == ()
    assert len(restarts) == 1


def test_summary_names_keys_never_values() -> None:
    report = secrets_supply.SupplyReport(
        service="platform/alerting",
        env="staging",
        changed=("FEISHU_WEBHOOK_URL",),
        missing=("FEISHU_APP_ID",),
        notes=("1Password lacks ['FEISHU_APP_ID']",),
    )
    text = report.summary()
    assert "FEISHU_WEBHOOK_URL" in text and "MISSING=['FEISHU_APP_ID']" in text
    assert "feishu.cn" not in text
