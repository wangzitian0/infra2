"""tools/secrets_reconcile + its CI wrapper: names only, findings gate, paging policy."""

from __future__ import annotations

import json
import re
import types
from pathlib import Path

import yaml

from infra2_sdk.capacity import CapacityReading
from infra2_sdk.secrets import SecretsError, WriteResult, op_item, vault_path

from libs.secrets_registry import SERVICES, Service
from tools import secrets_reconcile, secrets_reconcile_check

ROOT = Path(__file__).resolve().parents[2]


class MemoryBackend:
    def __init__(self, items=None, *, fail: str | None = None):
        self.items = {k: dict(v) for k, v in (items or {}).items()}
        self.fail = fail

    def read(self, path):
        if self.fail:
            raise SecretsError(self.fail)
        return dict(self.items.get(path, {}))

    def write(self, path, values):
        current = self.items.setdefault(path, {})
        changed = tuple(k for k, v in values.items() if current.get(k) != v)
        current.update(values)
        return WriteResult(changed=changed)


def _service(tmp_path) -> Service:
    manifest = {
        "contract_version": 2,
        "source": "t",
        "fields": [
            {"field": "mode", "env": "ALERT_DELIVERY_MODE", "source": "human"},
            {
                "field": "tok",
                "env": "BRIDGE_TOKEN",
                "source": "runtime",
                "sensitive": True,
            },
        ],
    }
    (tmp_path / "m.json").write_text(json.dumps(manifest), encoding="utf-8")
    return Service(
        str(tmp_path),
        "platform",
        "alerting",
        (str(tmp_path / "m.json"),),
        environments=("staging",),
    )


def test_reconcile_rows_carry_names_only_and_flag_drift(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(secrets_reconcile, "resolver_for", _resolver_for_root(tmp_path))
    store = MemoryBackend(
        {
            vault_path("platform", "staging", "alerting"): {
                "ALERT_DELIVERY_MODE": "feishu_webhook",
                "BRIDGE_TOKEN": "t",
                "LEFTOVER": "x",
            }
        }
    )
    human = MemoryBackend(
        {
            op_item("platform", "staging", "alerting"): {
                "ALERT_DELIVERY_MODE": "feishu_app"
            }
        }
    )
    rows = secrets_reconcile.reconcile_stores(
        services=(service,), store=store, human=human
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["service"] == "platform/alerting" and row["env"] == "staging"
    assert row["ok"] is False
    assert "LEFTOVER" in row["unclassified"] and "ALERT_DELIVERY_MODE" in row["stale"]
    assert "feishu" not in json.dumps(rows)  # values never leave the store


def test_reconcile_survives_1password_outage_with_a_note(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(secrets_reconcile, "resolver_for", _resolver_for_root(tmp_path))
    store = MemoryBackend(
        {
            vault_path("platform", "staging", "alerting"): {
                "ALERT_DELIVERY_MODE": "feishu_webhook",
                "BRIDGE_TOKEN": "t",
            }
        }
    )
    rows = secrets_reconcile.reconcile_stores(
        services=(service,), store=store, human=MemoryBackend(fail="op: not signed in")
    )
    assert rows[0]["ok"] is True
    assert "1Password unavailable" in rows[0]["note"]


def test_previews_are_skipped_and_bootstrap_is_production_only(
    tmp_path, monkeypatch
) -> None:
    runner = next(s for s in SERVICES if s.id == "bootstrap/iac_runner")
    assert runner.environments == ("production",)
    preview = next(s for s in SERVICES if s.preview)
    monkeypatch.setattr(secrets_reconcile, "resolver_for", _resolver_for_root(tmp_path))
    rows = secrets_reconcile.reconcile_stores(
        services=(preview,), store=MemoryBackend(), human=MemoryBackend()
    )
    assert rows == []  # a preview reads another environment's path; nothing of its own


def test_capacity_report_uses_readings_and_flags_exceeded() -> None:
    from infra2_sdk.capacity import CLOUDFLARE_FREE_TIER

    limit = next(entry for entry in CLOUDFLARE_FREE_TIER if entry.window == "day")
    used = limit.limit + 500
    report = secrets_reconcile.capacity_report(
        readings=lambda: (CapacityReading(limit.name, used),)
    )
    assert [i.name for i in report.at_level("exceeded")] == [limit.name]
    assert (
        f"{limit.name} {used}/{limit.limit}/day exceeded"
        in secrets_reconcile.render([], report)
    )


def test_render_lists_findings_per_row() -> None:
    rows = [
        {
            "service": "platform/alerting",
            "env": "staging",
            "missing": ["FEISHU_APP_ID"],
            "empty": [],
            "unclassified": [],
            "stale": [],
            "ok": False,
        },
        {
            "service": "platform/redis",
            "env": "staging",
            "missing": [],
            "empty": [],
            "unclassified": [],
            "stale": [],
            "ok": True,
        },
    ]
    text = secrets_reconcile.render(rows, None, "cloudflare token unreadable")
    assert text.splitlines()[0] == "secrets reconcile: 1 ok, 1 with findings"
    assert "platform/alerting staging: missing=['FEISHU_APP_ID']" in text
    assert "capacity: unavailable (cloudflare token unreadable)" in text


def _resolver_for_root(root):
    from infra2_sdk.secrets import SecretsResolver

    from libs.secrets_registry import merged_manifest

    def resolver_for(service, env, *, store=None, human=None):
        return SecretsResolver(
            merged_manifest(service, root=Path("/")),
            project=service.project,
            service=service.service,
            env=env,
            store=store,
            human=human,
        )

    return resolver_for


# --- the CI wrapper -----------------------------------------------------------------


def test_check_runs_the_reconcile_inside_the_runner_over_its_env() -> None:
    args = secrets_reconcile_check.ssh_args(
        {
            "INFRA2_WATCHDOG_SSH_HOST": "vps",
            "INFRA2_WATCHDOG_SSH_KEY_PATH": "/k",
            "INFRA2_WATCHDOG_SSH_PORT": "2222",
        }
    )
    assert args[:2] == ["ssh", "-o"] and "-i" in args and "2222" in args
    assert args[-2] == "root@vps"
    assert args[-1].startswith("docker exec iac-runner sh -c ")
    assert (
        ". /secrets/.env" in args[-1]
        and "tools/secrets_reconcile.py --json" in args[-1]
    )


def test_check_returns_a_transport_error_when_ssh_fails() -> None:
    def runner(args, **kwargs):
        return types.SimpleNamespace(
            returncode=255, stdout="", stderr="ssh: connect failed"
        )

    report = secrets_reconcile_check.run({"VPS_HOST": "vps"}, runner=runner)
    assert report["ok"] is False and "connect failed" in report["transport_error"]
    assert (
        secrets_reconcile_check.page_worthy_summary(report) == ""
    )  # hiccups never page


def test_check_parses_the_remote_json_and_pages_only_on_confirmed_findings() -> None:
    payload = {
        "ok": False,
        "stores": [
            {
                "service": "platform/alerting",
                "env": "staging",
                "missing": ["FEISHU_APP_ID"],
                "empty": [],
                "unclassified": [],
                "stale": [],
                "ok": False,
            },
            {
                "service": "platform/redis",
                "env": "staging",
                "missing": [],
                "empty": [],
                "unclassified": [],
                "stale": [],
                "ok": True,
            },
        ],
        "capacity": {
            "ok": False,
            "items": [
                {
                    "name": "kv_writes",
                    "used": 1200,
                    "limit": 1000,
                    "window": "day",
                    "level": "exceeded",
                },
                {
                    "name": "kv_reads",
                    "used": 50,
                    "limit": 100000,
                    "window": "day",
                    "level": "ok",
                },
            ],
        },
        "rendered": "x",
    }

    def runner(args, **kwargs):
        return types.SimpleNamespace(
            returncode=1, stdout="warning: noise\n" + json.dumps(payload), stderr=""
        )

    report = secrets_reconcile_check.run({"VPS_HOST": "vps"}, runner=runner)
    summary = secrets_reconcile_check.page_worthy_summary(report)
    assert "- platform/alerting staging: missing=['FEISHU_APP_ID']" in summary
    assert "- quota kv_writes 1200/1000 per day exceeded" in summary
    assert "kv_reads" not in summary and "platform/redis" not in summary


def test_ops_checks_schedules_the_reconcile_and_alerts_out_of_band() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/ops-checks.yml").read_text(encoding="utf-8")
    )
    assert {"cron": "27 8 * * *"} in workflow["on"]["schedule"]
    assert (
        "secrets-reconcile"
        in workflow["on"]["workflow_dispatch"]["inputs"]["task"]["options"]
    )
    job = workflow["jobs"]["secrets-reconcile"]
    assert "27 8 * * *" in job["if"]
    steps = {step["name"]: step for step in job["steps"]}
    assert (
        "python -m tools.secrets_reconcile_check"
        in steps["Run the secrets reconcile inside the iac-runner"]["run"]
    )
    alert = steps["Alert on confirmed findings"]
    assert (
        "page_worthy_summary" in alert["run"]
        and "deliver_out_of_band_alert" in alert["run"]
    )
    assert (
        "'27 8 * * *' && 'infra2-secrets-reconcile'" in workflow["concurrency"]["group"]
    )


def test_runner_image_pins_the_same_sdk_wheel_as_pyproject() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    requirements = (ROOT / "bootstrap/06.iac_runner/requirements.txt").read_text(
        encoding="utf-8"
    )
    wanted = re.search(r'"infra2-sdk @ (https://\S+?)"', pyproject).group(1)
    pinned = re.search(r"^infra2-sdk @ (https://\S+)$", requirements, re.M)
    assert pinned and pinned.group(1) == wanted, (
        "runner image and workspace must run the same SDK"
    )
