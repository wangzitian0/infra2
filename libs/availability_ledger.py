"""Availability ledger: schema, aggregation, and positive-proof reporting.

The Cloudflare watchdog records per-signal success + failure counts every run
into a rolling daily ledger. This module is the single home for how that ledger
is *read*: aggregating days into per-signal uptime, deriving the overall
availability, and rendering the weekly positive-proof report. Keeping the logic
here (pure, no I/O) means both the CLI runner and the tests exercise the exact
same code.

The ledger is the positive half of the loop: failure-only alerts cannot prove a
service was up, so this must count successes *and* refuse to report a degraded
signal as healthy. See ``docs/ssot/ops.availability-ledger.md``.

Storage contract (owned by the Worker):
- Hot window: Cloudflare KV key ``ledger:YYYY-MM-DD`` for the last
  ``LEDGER_RETENTION_DAYS`` days.
- Cold archive: Cloudflare R2 object ``watchdog-ledger/YYYY-MM-DD.json`` written
  once when the day rolls over.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

LEDGER_RETENTION_DAYS = 21
R2_LEDGER_PREFIX = "watchdog-ledger/"
WORST_SIGNALS_SHOWN = 5


def _coerce_count(value: Any) -> int:
    """Read a non-negative integer count, treating junk as zero."""
    try:
        count = int(value)
    except (TypeError, ValueError):
        return 0
    return count if count > 0 else 0


def _iter_days(ledger: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    days = ledger.get("ledger")
    if not isinstance(days, list):
        return []
    return [day for day in days if isinstance(day, Mapping)]


def summarize_ledger(ledger: Mapping[str, Any]) -> dict[str, Any]:
    """Aggregate per-signal success/failure across all ledger days.

    A signal is "perfect" only when it recorded zero failures. Malformed days or
    signal entries are ignored rather than trusted, so corrupt input can never
    inflate availability.
    """
    signals: dict[str, dict[str, Any]] = {}
    total_runs = 0
    for day in _iter_days(ledger):
        total_runs += _coerce_count(day.get("runs"))
        day_signals = day.get("signals")
        if not isinstance(day_signals, Mapping):
            continue
        for signal_id, counts in day_signals.items():
            if not isinstance(counts, Mapping):
                continue
            entry = signals.setdefault(
                signal_id, {"ok": 0, "fail": 0, "severity": "", "last_domain": ""}
            )
            entry["ok"] += _coerce_count(counts.get("ok"))
            entry["fail"] += _coerce_count(counts.get("fail"))
            entry["severity"] = counts.get("severity") or entry["severity"]
            if counts.get("lastDomain"):
                entry["last_domain"] = counts["lastDomain"]

    per_signal = []
    total_ok = 0
    total_checks = 0
    for signal_id, entry in signals.items():
        checks = entry["ok"] + entry["fail"]
        total_ok += entry["ok"]
        total_checks += checks
        per_signal.append(
            {
                "id": signal_id,
                "uptime_pct": _uptime_pct(entry["ok"], checks),
                "ok": entry["ok"],
                "fail": entry["fail"],
                "severity": entry["severity"],
                "last_domain": entry["last_domain"],
            }
        )

    per_signal.sort(key=lambda item: (item["uptime_pct"], -item["fail"]))
    perfect = [signal for signal in per_signal if signal["fail"] == 0]
    return {
        "as_of": ledger.get("as_of", "latest"),
        "window_days": _coerce_count(ledger.get("window_days")) or len(list(_iter_days(ledger))),
        "total_runs": total_runs,
        "signal_count": len(per_signal),
        "perfect_count": len(perfect),
        "overall_uptime_pct": _uptime_pct(total_ok, total_checks),
        "signals": per_signal,
    }


def _uptime_pct(ok: int, checks: int) -> float:
    """Availability as a percentage; no checks means nothing to disprove (100%)."""
    if checks <= 0:
        return 100.0
    return round((ok / checks) * 100.0, 3)


def build_report_message(summary: Mapping[str, Any]) -> str:
    """Render the weekly positive-proof report for Lark."""
    lines = [
        "[STABILITY] Infra2 weekly availability — positive proof",
        f"As of {summary['as_of']} | window {summary['window_days']}d | "
        f"{summary['total_runs']} probe runs",
        f"Overall availability: {summary['overall_uptime_pct']}%",
        f"Signals at 100%: {summary['perfect_count']}/{summary['signal_count']}",
    ]
    degraded = [s for s in summary["signals"] if s["fail"] > 0][:WORST_SIGNALS_SHOWN]
    if degraded:
        lines.append("Lowest availability:")
        for signal in degraded:
            domain = f" [{signal['last_domain']}]" if signal["last_domain"] else ""
            checks = signal["ok"] + signal["fail"]
            lines.append(
                f"  {signal['id']}: {signal['uptime_pct']}% "
                f"({signal['fail']} fail/{checks} checks){domain}"
            )
    else:
        lines.append("All monitored signals held 100% availability this window. ✅")
    return "\n".join(lines)
