"""VPS-side availability ledger: per-signal daily ok/fail counts (#904).

Counting successes is heavy, recurring work, so it lives with the probes on the VPS
(ops.observability.md §1.1). The probe runner calls :func:`record_probe_round` once
per round; the weekly report reads the file over SSH and adds the outage intervals
the Cloudflare Worker recorded, because the VPS cannot count its own downtime
(``libs.availability_ledger.apply_outages``).

Pure stdlib: the runner image and the weekly GitHub job both import this.

File (one per environment, on the host under :data:`HOST_LEDGER_DIR`)::

    {
      "schema": 1,
      "environment": "production",
      "updated_at": 1790208000,          # epoch seconds of the last recorded round
      "days": {
        "2026-09-24": {
          "runs": 1440,
          "signals": {
            "production:vault-public-route":
              {"ok": 1439, "fail": 1, "severity": "critical", "lastDomain": "public-route"}
          }
        }
      }
    }
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = 1
RETENTION_DAYS = 21
LEDGER_FILE_ENV = "INFRA_PROBE_LEDGER_FILE"
LEDGER_FILE_NAME = "availability-ledger.json"
# Host directory the probe runner's ledger is bind-mounted from
# (platform/12.alerting/compose.yaml); non-production appends "-<env>".
HOST_LEDGER_DIR = "/var/lib/infra2-availability-ledger"


def host_ledger_path(environment: str) -> str:
    """Where ``environment``'s ledger lives on the host."""
    suffix = "" if environment == "production" else f"-{environment}"
    return f"{HOST_LEDGER_DIR}{suffix}/{LEDGER_FILE_NAME}"


def ledger_path(state_path: Path, env: Mapping[str, str] | None = None) -> Path:
    """The configured ledger file, else one beside the runner's state file."""
    configured = (env if env is not None else os.environ).get(LEDGER_FILE_ENV, "")
    return (
        Path(configured.strip())
        if configured.strip()
        else state_path.parent / LEDGER_FILE_NAME
    )


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def new_ledger(environment: str) -> dict[str, Any]:
    return {"schema": SCHEMA, "environment": environment, "updated_at": 0, "days": {}}


def record_round(
    ledger: dict[str, Any],
    results_by_group: Mapping[str, Iterable[Mapping[str, Any]]],
    environment: str,
    now: float,
) -> dict[str, Any]:
    """Add one probe round: one ok or fail per probe result, one run for the day.

    ``results_by_group`` is the runner's ``{group: [ProbeResult.to_dict()]}``,
    taken before cascade suppression: a suppressed symptom still failed.
    """
    days = ledger.setdefault("days", {})
    day = days.setdefault(utc_day(now), {"runs": 0, "signals": {}})
    day["runs"] = int(day.get("runs", 0)) + 1
    signals = day.setdefault("signals", {})
    for group, results in results_by_group.items():
        for result in results:
            spec = result.get("spec") or {}
            name = str(spec.get("name") or "").strip()
            if not name:
                continue
            entry = signals.setdefault(
                f"{environment}:{name}",
                {"ok": 0, "fail": 0, "severity": "", "lastDomain": ""},
            )
            if result.get("ok") is True:
                entry["ok"] += 1
            else:
                entry["fail"] += 1
                entry["lastDomain"] = group
            entry["severity"] = str(spec.get("severity") or entry.get("severity") or "")
    ledger["updated_at"] = int(now)
    return ledger


def prune(
    ledger: dict[str, Any], now: float, retention_days: int = RETENTION_DAYS
) -> dict[str, Any]:
    """Drop days older than the retention window (today counts as day 1)."""
    oldest = (
        datetime.fromtimestamp(now, UTC) - timedelta(days=retention_days - 1)
    ).strftime("%Y-%m-%d")
    days = ledger.get("days") or {}
    ledger["days"] = {date: day for date, day in days.items() if date >= oldest}
    return ledger


def load_ledger(path: Path, environment: str) -> dict[str, Any]:
    """The ledger at ``path``; a missing file starts a new one. A corrupt file
    also starts over, loudly, and is kept beside it for inspection."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return new_ledger(environment)
    try:
        ledger = json.loads(raw)
        if not isinstance(ledger, dict) or not isinstance(ledger.get("days"), dict):
            raise ValueError("not a ledger document")
    except ValueError as exc:
        path.replace(path.with_suffix(".corrupt.json"))
        print(
            f"availability ledger at {path} was unreadable ({exc}); starting over",
            flush=True,
        )
        return new_ledger(environment)
    return ledger


def write_ledger(path: Path, ledger: Mapping[str, Any]) -> None:
    """Replace the file atomically, world-readable so the host's SSH user can read it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(
        json.dumps(ledger, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def record_probe_round(
    results_by_group: Mapping[str, Iterable[Mapping[str, Any]]],
    environment: str,
    state_path: Path,
    now: float,
) -> None:
    """The probe runner's one call per round. Never raises: a ledger problem must
    not fail the probe loop, whose failure the heartbeat reports as an outage."""
    try:
        path = ledger_path(state_path)
        ledger = load_ledger(path, environment)
        record_round(ledger, results_by_group, environment, now)
        prune(ledger, now)
        write_ledger(path, ledger)
    except Exception as exc:  # noqa: BLE001 - see docstring
        print(
            f"availability ledger write failed: {type(exc).__name__}: {exc}", flush=True
        )


def to_report_days(ledger: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The ledger's days in the ``libs.availability_ledger`` report shape."""
    days = ledger.get("days") if isinstance(ledger, Mapping) else None
    if not isinstance(days, Mapping):
        return []
    return [
        {"date": date, "runs": day.get("runs", 0), "signals": day.get("signals", {})}
        for date, day in sorted(days.items())
        if isinstance(day, Mapping)
    ]
