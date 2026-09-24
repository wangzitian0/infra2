"""VPS-side availability ledger: per-signal daily ok/fail counts and presence (#904).

Counting successes is heavy, recurring work, so it lives with the probes on the VPS
(ops.observability.md §1.1). The probe runner calls :func:`record_probe_round` once
per round. Each round also marks its 5-minute slot as present, so the weekly report
can count the time no round ran at all (a crashed runner, a host that was down for
less than the Cloudflare staleness window, a deploy restart) as unavailable
(:func:`gap_intervals`); the Cloudflare Worker's outage edges add the time the VPS
was unreachable (``libs.availability_ledger.apply_unavailability``).

Pure stdlib: the runner image and the weekly GitHub job both import this.

File (one per environment, on the host under :data:`HOST_LEDGER_DIR`)::

    {
      "schema": 2,
      "environment": "production",
      "started_at": 1790150000,          # first round this file recorded
      "updated_at": 1790208000,          # last recorded round
      "days": {
        "2026-09-24": {
          "runs": 1309,
          "presence": "ffff...",         # 288 bits, one per 5-minute slot, hex
          "signals": {
            "production:vault-public-route":
              {"ok": 1308, "fail": 1, "severity": "critical", "lastDomain": "public-route"}
          }
        }
      }
    }
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = 2
RETENTION_DAYS = 21
LEDGER_FILE_ENV = "INFRA_PROBE_LEDGER_FILE"
LEDGER_FILE_NAME = "availability-ledger.json"
# Host directory the probe runner's ledger is bind-mounted from
# (platform/12.alerting/compose.yaml); non-production appends "-<env>".
HOST_LEDGER_DIR = "/var/lib/infra2-availability-ledger"
# Presence resolution. The loop starts a round every ~66 s (60 s sleep + the
# probes), and a slow all-timeouts round can take minutes, so a 1-minute slot
# would read a healthy loop as absent; a 5-minute slot is empty only if no round
# started for 5 minutes. Gaps shorter than a slot can go unseen.
SLOT_SECONDS = 300
SLOTS_PER_DAY = 86400 // SLOT_SECONDS
_PRESENCE_HEX = SLOTS_PER_DAY // 4
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HEX = re.compile(r"^[0-9a-f]*$")


class CorruptLedger(ValueError):
    """The file parses but is not a ledger this code can safely add to."""


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


def _day_start(date: str) -> float:
    return datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()


def new_ledger(environment: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "environment": environment,
        "started_at": 0,
        "updated_at": 0,
        "days": {},
    }


def _new_day() -> dict[str, Any]:
    return {"runs": 0, "presence": "0" * _PRESENCE_HEX, "signals": {}}


def present_slots(presence: str) -> set[int]:
    """Slot indexes whose bit is set in a day's presence bitmap."""
    bits = int(presence, 16) if presence else 0
    return {slot for slot in range(SLOTS_PER_DAY) if bits >> slot & 1}


def _mark_present(day: dict[str, Any], now: float) -> None:
    slot = int(now - _day_start(utc_day(now))) // SLOT_SECONDS
    bits = int(day.get("presence") or "0", 16) | (1 << slot)
    day["presence"] = f"{bits:0{_PRESENCE_HEX}x}"


def record_round(
    ledger: dict[str, Any],
    results_by_group: Mapping[str, Iterable[Mapping[str, Any]]],
    environment: str,
    now: float,
) -> dict[str, Any]:
    """Add one probe round: one ok or fail per probe result, one run for the day,
    and presence for the round's slot.

    ``results_by_group`` is the runner's ``{group: [ProbeResult.to_dict()]}``,
    taken before cascade suppression: a suppressed symptom still failed.
    """
    days = ledger.setdefault("days", {})
    day = days.setdefault(utc_day(now), _new_day())
    day["runs"] = int(day.get("runs", 0)) + 1
    _mark_present(day, now)
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
    if not ledger.get("started_at"):
        ledger["started_at"] = int(now)
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


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate(ledger: Any) -> None:
    """Raise :class:`CorruptLedger` unless ``ledger`` has exactly the types this
    module writes: a valid-JSON file of the wrong shape would otherwise make every
    round raise, and the ledger would silently stop counting."""
    if not isinstance(ledger, dict):
        raise CorruptLedger("not an object")
    if ledger.get("schema") != SCHEMA:
        raise CorruptLedger(f"schema {ledger.get('schema')!r}, expected {SCHEMA}")
    for field in ("started_at", "updated_at"):
        if not _count(ledger.get(field)):
            raise CorruptLedger(f"{field} is not a non-negative integer")
    days = ledger.get("days")
    if not isinstance(days, dict):
        raise CorruptLedger("days is not an object")
    for date, day in days.items():
        if not isinstance(date, str) or not _DATE.match(date):
            raise CorruptLedger(f"day key {date!r}")
        if not isinstance(day, dict) or not _count(day.get("runs")):
            raise CorruptLedger(f"day {date} is not an object with runs")
        presence = day.get("presence")
        if (
            not isinstance(presence, str)
            or len(presence) != _PRESENCE_HEX
            or not _HEX.match(presence)
        ):
            raise CorruptLedger(f"day {date} presence")
        signals = day.get("signals")
        if not isinstance(signals, dict):
            raise CorruptLedger(f"day {date} signals")
        for sid, entry in signals.items():
            if (
                not isinstance(entry, dict)
                or not _count(entry.get("ok"))
                or not _count(entry.get("fail"))
                or not isinstance(entry.get("severity", ""), str)
                or not isinstance(entry.get("lastDomain", ""), str)
            ):
                raise CorruptLedger(f"day {date} signal {sid!r}")


def load_ledger(path: Path, environment: str) -> dict[str, Any]:
    """The ledger at ``path``; a missing file starts a new one. A file that is not
    valid JSON, or not a ledger of this schema, also starts over, loudly, and is
    kept beside it for inspection."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return new_ledger(environment)
    try:
        ledger = json.loads(raw)
        validate(ledger)
    except ValueError as exc:  # JSONDecodeError and CorruptLedger
        path.replace(path.with_suffix(".corrupt.json"))
        print(
            f"availability ledger at {path} was unreadable ({exc}); starting over",
            flush=True,
        )
        return new_ledger(environment)
    return ledger


def write_ledger(path: Path, ledger: Mapping[str, Any]) -> None:
    """Replace the file atomically and durably, world-readable so the host's SSH
    user can read it: the data reaches the disk before the rename publishes it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    data = json.dumps(ledger, sort_keys=True, separators=(",", ":")).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, data)
        os.fchmod(fd, 0o644)
        os.fsync(fd)
    finally:
        os.close(fd)
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


def gap_intervals(ledger: Mapping[str, Any], now: float) -> list[tuple[float, float]]:
    """Every stretch, in epoch seconds, of whole slots in which no round ran.

    Covered time runs from the slot of the first round this file recorded (or the
    start of its oldest kept day) to the start of the current, unfinished slot.
    A day missing inside that span is a whole-day gap.
    """
    days = ledger.get("days") if isinstance(ledger, Mapping) else None
    if not isinstance(days, Mapping) or not days:
        return []
    first_day = min(days)
    started = max(float(ledger.get("started_at") or 0), _day_start(first_day))
    begin = started - started % SLOT_SECONDS
    end = now - now % SLOT_SECONDS
    bits = {
        date: int(str(day.get("presence") or "0"), 16)
        for date, day in days.items()
        if isinstance(day, Mapping)
    }
    gaps: list[tuple[float, float]] = []
    slot_start = begin
    while slot_start < end:
        date = utc_day(slot_start)
        index = int(slot_start - _day_start(date)) // SLOT_SECONDS
        if not bits.get(date, 0) >> index & 1:
            if gaps and gaps[-1][1] == slot_start:
                gaps[-1] = (gaps[-1][0], slot_start + SLOT_SECONDS)
            else:
                gaps.append((slot_start, slot_start + SLOT_SECONDS))
        slot_start += SLOT_SECONDS
    return gaps
