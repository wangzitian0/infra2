"""Deterministic dry-run recall drill for all 8 watchdog failure classes.

Simulates each failure class from docs/ssot/watchdog-signals.yaml,
verifies pager layer assignment and PAGER_FIELDS formatting.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from common.alerting import PAGER_FIELDS, PagerItem
from common.observability.watchdog_audit import LAYER_OF_OWNER, _load_inventory

REPO_ROOT = Path(__file__).resolve().parents[2]
SIGNALS_FILE = REPO_ROOT / "docs" / "ssot" / "watchdog-signals.yaml"

SIMULATED_FAILURES: dict[str, dict[str, Any]] = {
    "host-reachability": {
        "signal_id": "production.dokploy.public-route",
        "level": "P0",
        "target": "dokploy-public-route",
        "symptom": "External HTTP probe returned status 522.",
        "since": "2026-10-28 12:00:00 UTC+8",
        "impact": "Host down or public route unreachable from outside.",
        "action": "Inspect cloudflare edge logs and verify VPS network route.",
        "runbook": "docs/ssot/ops.observability.md#sop-001",
    },
    "alert-pipeline": {
        "signal_id": "production.alerting-probes.heartbeat",
        "level": "P1",
        "target": "platform-alerting-probes",
        "symptom": "Probe loop reports bridge delivery failing for 42 minutes.",
        "since": "2026-10-28 12:00:00 UTC+8",
        "impact": "Alert delivery pipeline broken or stalled.",
        "action": "Restart feishu alert bridge and verify webhook reachability.",
        "runbook": "docs/ssot/ops.observability.md#sop-002",
    },
    "watchdog-liveness": {
        "signal_id": "global.cloudflare-worker-status.github",
        "level": "P0",
        "target": "infra2-cloudflare-watchdog",
        "symptom": "Cloudflare worker status document is older than 7200 seconds.",
        "since": "2026-10-28 12:00:00 UTC+8",
        "impact": "Out-of-band watchdog stopped running.",
        "action": "Inspect cloudflare worker cron triggers and error telemetry.",
        "runbook": "docs/ssot/ops.observability.md#sop-003",
    },
    "service-health": {
        "signal_id": "production.signoz.public-route",
        "level": "P1",
        "target": "finance-report-backend",
        "symptom": "Backend emitted more than 10 error logs in 15 minutes.",
        "since": "2026-10-28 12:00:00 UTC+8",
        "impact": "Application service error rate degraded.",
        "action": "Check application log tail and database connection pool.",
        "runbook": "docs/ssot/ops.observability.md#sop-004b",
    },
    "data-protection": {
        "signal_id": "production.infra2-backup-production.github",
        "level": "P1",
        "target": "platform/postgres",
        "symptom": "Daily database backup artifact is stale or missing on remote.",
        "since": "2026-10-28 12:00:00 UTC+8",
        "impact": "Disaster recovery RPO target breached.",
        "action": "Run manual backup trigger and check object storage credentials.",
        "runbook": "docs/ssot/ops.storage.md#sop-001",
    },
    "config-drift": {
        "signal_id": "global.infra2.facet-reconcile",
        "level": "P2",
        "target": "platform/vault",
        "symptom": "Vault self-refresh audit detected token drift.",
        "since": "2026-10-28 12:00:00 UTC+8",
        "impact": "Service secrets diverge from canonical configuration.",
        "action": "Execute vault reconcile workflow to sync credentials.",
        "runbook": "docs/ssot/db.vault-integration.md#sop-002",
    },
    "peer-scheduler": {
        "signal_id": "global.truealpha-scheduler-liveness.github",
        "level": "P1",
        "target": "truealpha-scheduler",
        "symptom": "Peer repository scheduler heartbeat missing for 60 minutes.",
        "since": "2026-10-28 12:00:00 UTC+8",
        "impact": "Peer scheduled execution stalled.",
        "action": "Verify prefect agent status and inspect prefect worker logs.",
        "runbook": "docs/ssot/ops.observability.md#sop-005",
    },
    "scheduled-job-start": {
        "signal_id": "global.infra2-scheduled-job-start.github",
        "level": "P1",
        "target": "ops-checks/host-hygiene",
        "symptom": "Scheduled ops-checks workflow failed before job execution started.",
        "since": "2026-10-28 12:00:00 UTC+8",
        "impact": "Automated operations checks missed execution.",
        "action": "Inspect github actions queue status and runner health.",
        "runbook": "docs/ssot/ops.observability.md#sop-006",
    },
}


@dataclass(frozen=True)
class DrillClassResult:
    """Evaluation result for one simulated failure class."""

    failure_class: str
    expected_layer: str
    actual_layer: str
    layer_verified: bool
    payload: dict[str, str]
    fields_verified: bool
    status: str
    signal_id: str = ""


@dataclass(frozen=True)
class RecallDrillReport:
    """Report summarizing the 8-class recall drill outcome."""

    status: str
    dry_run: bool
    total_classes: int
    classes_evaluated: list[str]
    results: dict[str, dict[str, Any]]


def load_failure_classes(
    signals_path: Path = SIGNALS_FILE,
) -> dict[str, str]:
    """Load failure class pager layer assignments from SSOT signals file."""
    raw = yaml.safe_load(signals_path.read_text(encoding="utf-8"))
    classes = raw.get("failure_classes", {})
    return {class_name: info.get("pager", "") for class_name, info in classes.items()}


def run_recall_drill(
    *,
    dry_run: bool = True,
    inventory: dict[str, Any] | None = None,
) -> RecallDrillReport:
    """Run deterministic recall drill for all 8 watchdog failure classes.

    Verify signal routing, pager layer assignment, and alert fields.
    Refuse execution when dry_run is False.
    """
    if not dry_run:
        raise ValueError(
            "Recall drill only supports dry_run=True to protect ambient services."
        )

    if inventory is None:
        inventory = _load_inventory()

    raw_classes = inventory.get("failure_classes") or {}
    failure_classes = {
        name: spec.get("pager", "") if isinstance(spec, dict) else str(spec)
        for name, spec in raw_classes.items()
    }
    if not failure_classes:
        failure_classes = load_failure_classes()

    signals = inventory.get("signals") or []
    signals_by_id = {
        str(sig.get("signal_id")): sig for sig in signals if sig.get("signal_id")
    }

    classes_match = set(failure_classes.keys()) == set(SIMULATED_FAILURES.keys())
    results: dict[str, dict[str, Any]] = {}
    classes_evaluated: list[str] = []

    for failure_class, expected_layer in failure_classes.items():
        classes_evaluated.append(failure_class)
        sim = SIMULATED_FAILURES.get(failure_class)
        if not sim:
            result_obj = DrillClassResult(
                failure_class=failure_class,
                expected_layer=expected_layer,
                actual_layer="",
                layer_verified=False,
                payload={},
                fields_verified=False,
                status="fail",
                signal_id="",
            )
            results[failure_class] = asdict(result_obj)
            continue

        signal_id = str(sim.get("signal_id", ""))
        sig = signals_by_id.get(signal_id)
        if sig is None:
            actual_layer = ""
            layer_verified = False
        else:
            owner = str(sig.get("primary_owner", ""))
            signal_fc = sig.get("failure_class") or (
                "service-health" if owner == "internal" else None
            )
            actual_layer = str(LAYER_OF_OWNER.get(owner) or sig.get("layer", ""))
            layer_verified = bool(
                actual_layer
                and actual_layer == expected_layer
                and signal_fc == failure_class
            )

        item = PagerItem(
            level=sim.get("level", "P1"),
            environment="production",
            target=sim.get("target", failure_class),
            symptom=sim.get("symptom", f"Simulated failure for {failure_class}."),
            since=sim.get("since", "2026-10-28 12:00:00 UTC+8"),
            impact=sim.get("impact", f"Impact for {failure_class}."),
            action=sim.get("action", f"Action for {failure_class}."),
            runbook=sim.get("runbook", "docs/ssot/ops.observability.md"),
            log="",
        )
        fields = dict(item.fields())
        fields_verified = all(
            field in fields for field in PAGER_FIELDS if field != "日志"
        )
        class_status = "pass" if (layer_verified and fields_verified) else "fail"
        result_obj = DrillClassResult(
            failure_class=failure_class,
            expected_layer=expected_layer,
            actual_layer=actual_layer,
            layer_verified=layer_verified,
            payload=fields,
            fields_verified=fields_verified,
            status=class_status,
            signal_id=signal_id,
        )
        results[failure_class] = asdict(result_obj)

    overall_status = (
        "pass"
        if classes_match and all(r["status"] == "pass" for r in results.values())
        else "fail"
    )
    return RecallDrillReport(
        status=overall_status,
        dry_run=True,
        total_classes=len(results),
        classes_evaluated=classes_evaluated,
        results=results,
    )
