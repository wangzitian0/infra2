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


# --- the KV write trend beside the verdict (2026-09-15: 1198/1000 with no history) ------

# Cloudflare's per-day KV puts for the account, 2026-09-09 .. 2026-09-16 10:00Z.
MEASURED_KV_WRITES = {
    "2026-09-09": 288,
    "2026-09-10": 298,
    "2026-09-11": 286,
    "2026-09-12": 288,
    "2026-09-13": 284,
    "2026-09-14": 290,
    "2026-09-15": 1198,
    "2026-09-16": 1157,
}


def _measured_fetch(calls):
    import datetime as dt

    def fetch(day: dt.date):
        calls.append(day.isoformat())
        used = MEASURED_KV_WRITES.get(day.isoformat())
        if used is None:
            return ()  # Cloudflare answered no account row
        return (
            CapacityReading("cloudflare.kv.read", 5300),
            CapacityReading("cloudflare.kv.write", used),
            CapacityReading("cloudflare.workers.requests", 6000),
        )

    return fetch


def test_capacity_section_reports_the_write_trend_around_the_verdict() -> None:
    import datetime as dt

    calls: list[str] = []
    report, trend, error = secrets_reconcile.capacity_section(
        today=dt.date(2026, 9, 16), fetch=_measured_fetch(calls)
    )
    assert error is None
    assert [i.name for i in report.at_level("exceeded")] == ["cloudflare.kv.write"]
    assert trend["name"] == "cloudflare.kv.write" and trend["limit"] == 1000
    assert [row["date"] for row in trend["days"]] == sorted(MEASURED_KV_WRITES)
    assert [row["used"] for row in trend["days"]] == [
        MEASURED_KV_WRITES[day] for day in sorted(MEASURED_KV_WRITES)
    ]
    assert [row["partial"] for row in trend["days"]] == [False] * 7 + [True]
    # one analytics query per day: yesterday's serves the verdict and the trend
    assert sorted(calls) == sorted(MEASURED_KV_WRITES)
    assert trend["rendered"] == (
        "cloudflare.kv.write/day (limit 1000; ! = over): 09-09 288 · 09-10 298 · "
        "09-11 286 · 09-12 288 · 09-13 284 · 09-14 290 · 09-15 1198! · "
        "09-16 1157! so far"
    )
    text = secrets_reconcile.render([], report, None, trend)
    assert "capacity: cloudflare.kv.write 1198/1000/day exceeded" in text
    assert "  cloudflare.kv.write/day (limit 1000; ! = over): 09-09 288" in text


def test_trend_tells_no_answer_from_no_writes() -> None:
    import datetime as dt

    def fetch(day):
        if day == dt.date(2026, 9, 14):
            return ()
        return (CapacityReading("cloudflare.workers.requests", 1),)

    rows = secrets_reconcile.reading_trend(fetch, today=dt.date(2026, 9, 16), days=2)
    assert [(row["date"], row["used"]) for row in rows] == [
        ("2026-09-14", None),
        ("2026-09-15", 0),
        ("2026-09-16", 0),
    ]
    rendered = secrets_reconcile.render_trend(
        {"name": "cloudflare.kv.write", "limit": 1000, "days": rows}
    )
    assert rendered.endswith("09-14 ? · 09-15 0 · 09-16 0 so far")


def test_an_unreadable_trend_never_hides_the_verdict() -> None:
    import datetime as dt

    measured = _measured_fetch([])

    def fetch(day):
        if day == dt.date(2026, 9, 10):
            raise RuntimeError("Cloudflare analytics failed with HTTP 429")
        return measured(day)

    report, trend, error = secrets_reconcile.capacity_section(
        today=dt.date(2026, 9, 16), fetch=fetch
    )
    assert report.at_level("exceeded") and trend is None
    assert error == "Cloudflare analytics failed with HTTP 429"
    text = secrets_reconcile.render([], report, None, None, error)
    assert "capacity: cloudflare.kv.write 1198/1000/day exceeded" in text
    assert "cloudflare.kv.write/day trend unavailable (Cloudflare analytics" in text


def test_build_report_carries_the_trend(monkeypatch) -> None:
    import datetime as dt

    monkeypatch.setattr(secrets_reconcile, "reconcile_stores", lambda: [])
    section = secrets_reconcile.capacity_section(
        today=dt.date(2026, 9, 15), fetch=_measured_fetch([])
    )
    monkeypatch.setattr(secrets_reconcile, "capacity_section", lambda: section)
    report = secrets_reconcile.build_report()
    assert report["ok"] is True  # 09-14 (290) is yesterday here
    assert report["capacity_trend"]["days"][-1] == {
        "date": "2026-09-15",
        "used": 1198,
        "partial": True,
    }
    assert report["capacity_trend_error"] is None
    assert "capacity: ok" in report["rendered"]
    assert "09-15 1198! so far" in report["rendered"]
    json.dumps(report)  # the ops-check job reads it back as JSON


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


def test_an_exceeded_quota_pages_with_its_trend() -> None:
    report = {
        "ok": False,
        "stores": [],
        "capacity": {
            "ok": False,
            "items": [
                {
                    "name": "cloudflare.kv.write",
                    "used": 1198,
                    "limit": 1000,
                    "window": "day",
                    "level": "exceeded",
                },
                {
                    "name": "cloudflare.kv.delete",
                    "used": 1,
                    "limit": 1000,
                    "window": "day",
                    "level": "ok",
                },
            ],
        },
        "capacity_trend": {
            "name": "cloudflare.kv.write",
            "limit": 1000,
            "days": [],
            "rendered": "cloudflare.kv.write/day (limit 1000; ! = over): 09-15 1198!",
        },
    }
    assert secrets_reconcile_check.page_worthy_summary(report) == (
        "- quota cloudflare.kv.write 1198/1000 per day exceeded\n"
        "  cloudflare.kv.write/day (limit 1000; ! = over): 09-15 1198!"
    )
    # a report from a runner without the trend still pages the quota line alone
    del report["capacity_trend"]
    assert secrets_reconcile_check.page_worthy_summary(report) == (
        "- quota cloudflare.kv.write 1198/1000 per day exceeded"
    )


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


def test_unclassified_leftovers_are_reported_but_never_page() -> None:
    report = {
        "ok": False,
        "stores": [
            {
                "service": "platform/prefect",
                "env": "staging",
                "missing": [],
                "empty": [],
                "unclassified": ["postgres_password"],
                "stale": [],
                "ok": False,
            },
            {
                "service": "truealpha/data_engine",
                "env": "staging",
                "missing": [],
                "empty": [],
                "unclassified": ["RELEASE_MANIFEST_ID"],
                "stale": ["SEC_USER_AGENT"],
                "ok": False,
            },
        ],
        "capacity": {"ok": True, "items": []},
    }
    summary = secrets_reconcile_check.page_worthy_summary(report)
    assert "platform/prefect" not in summary
    assert "truealpha/data_engine staging: stale=['SEC_USER_AGENT']" in summary
    assert "RELEASE_MANIFEST_ID" not in summary


# --- an application credential is never the object store's root credential (#677) -------


def test_over_privileged_finds_a_root_credential_in_an_application_store() -> None:
    # fixture values, built rather than written out, so no line in this file has the
    # shape of a credential assignment
    prod, stg, scoped = (f"fixture-{name}" for name in ("a", "b", "c"))
    roots = {
        "production": {"root_user": "admin", "root_password": prod},
        "staging": {"root_user": "admin", "root_password": stg},
    }
    documents = {
        # the object store's own path legitimately holds it
        "platform/minio|production": {"root_user": "admin", "root_password": prod},
        # an application holding the root credential, twice, under different names
        "finance_report/app|staging": {
            "S3_ACCESS_KEY": "admin",
            "S3_SECRET_KEY": prod,
            "S3_PUBLIC_SECRET_KEY": prod,
            "SECRET_KEY": "unrelated",
        },
        # a scoped credential is fine
        "truealpha/app|production": {
            "S3_ACCESS_KEY": "truealpha_raw",
            "S3_SECRET_KEY": scoped,
        },
    }
    findings = secrets_reconcile.over_privileged(documents, roots)
    assert findings == {
        "finance_report/app|staging": [
            "S3_ACCESS_KEY",
            "S3_PUBLIC_SECRET_KEY",
            "S3_SECRET_KEY",
        ]
    }
    # names only: no value from any store appears in the finding
    assert prod not in json.dumps(findings)


def test_over_privileged_pages_and_reads_as_its_own_finding() -> None:
    report = {
        "ok": False,
        "stores": [
            {
                "service": "finance_report/app",
                "env": "staging",
                "missing": [],
                "empty": [],
                "unclassified": [],
                "stale": [],
                "over_privileged": ["S3_SECRET_KEY"],
                "ok": False,
            }
        ],
        "capacity": {"ok": True, "items": []},
    }
    summary = secrets_reconcile_check.page_worthy_summary(report)
    assert "finance_report/app staging: over_privileged=['S3_SECRET_KEY']" in summary
