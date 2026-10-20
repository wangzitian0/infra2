"""Availability ledger: aggregation and positive-proof reporting.

This module is the single home for how the availability ledger is *read*:
aggregating days into per-signal uptime, counting the VPS's own outages as
failures, deriving the overall availability, and rendering the weekly
positive-proof report. Pure, no I/O: the CLI runner and the tests exercise the
exact same code.

The ledger is the positive half of the loop: failure-only alerts cannot prove a
service was up, so this must count successes *and* refuse to report a degraded
signal as healthy. See ``docs/ssot/ops.observability.md`` §6.

Sources (#904, ops.observability.md §1.1):
- Success and failure counts: the VPS probe runner, one file per environment
  (``common.observability.local_ledger``, which also owns the retention).
- VPS outages, which the VPS cannot record itself: the Cloudflare Worker's
  heartbeat down/up edges (``GET /outages``), applied by :func:`apply_outages`.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

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
    perfect = [
        signal
        for signal in per_signal
        if signal["fail"] == 0 and (signal["ok"] + signal["fail"]) > 0
    ]
    summary = {
        "as_of": ledger.get("as_of", "latest"),
        "window_days": _coerce_count(ledger.get("window_days"))
        or len(list(_iter_days(ledger))),
        "total_runs": total_runs,
        "signal_count": len(per_signal),
        "perfect_count": len(perfect),
        "overall_uptime_pct": _uptime_pct(total_ok, total_checks),
        "has_data": total_checks > 0,
        "signals": per_signal,
    }
    if isinstance(ledger.get("unavailability"), Mapping):
        summary["unavailability"] = dict(ledger["unavailability"])
    return summary


def outage_intervals(
    outages: Iterable[Any], *, environment: str, now: datetime
) -> tuple[list[tuple[float, float]], int]:
    """The Worker's outage records for ``environment`` as (start, end) epoch
    seconds, an open one ending now, plus how many records could not be read."""
    now_s = now.timestamp()
    intervals: list[tuple[float, float]] = []
    unreadable = 0
    for outage in outages:
        if not isinstance(outage, Mapping):
            unreadable += 1
            continue
        if outage.get("environment") != environment:
            continue
        start, end = outage.get("start"), outage.get("end")
        if not _is_number(start) or not (end is None or _is_number(end)):
            unreadable += 1
            continue
        stop = now_s if end is None else min(float(end) / 1000, now_s)
        if stop > float(start) / 1000:
            intervals.append((float(start) / 1000, stop))
    return intervals, unreadable


def _union(intervals: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for begin, stop in sorted(intervals):
        if merged and begin <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], stop))
        else:
            merged.append((begin, stop))
    return merged


def _measure(intervals: Iterable[tuple[float, float]], lo: float, hi: float) -> float:
    return sum(max(0.0, min(stop, hi) - max(begin, lo)) for begin, stop in intervals)


def apply_unavailability(
    ledger: Mapping[str, Any],
    *,
    environment: str,
    gaps: Iterable[tuple[float, float]],
    outages: Iterable[Any],
    check_interval_seconds: int,
    now: datetime,
) -> dict[str, Any]:
    """Count the time the VPS recorded nothing, or was unreachable, as failures.

    A probe runner that is down, restarting or cut off records no rounds, so its
    ledger alone reports the survivors of an outage as 100%. ``gaps`` are the
    slots with no round (``common.observability.local_ledger.gap_intervals``);
    ``outages`` the Worker's heartbeat outage records (epoch ms; ``end`` null while
    open). Their union -- overlapping time counts once -- adds, per UTC day inside
    the ledger window, one failed check per ``check_interval_seconds`` to every
    ``environment`` signal: the checks the runner would have made. A day the runner
    never recorded is added. An outage record that cannot be read is counted in
    ``unavailability.unreadable`` and shown in the report, never silently dropped.
    """
    days = [
        dict(day, signals=dict(day.get("signals") or {})) for day in _iter_days(ledger)
    ]
    if not days:
        return dict(ledger, ledger=days)
    prefix = f"{environment}:"
    signal_ids = sorted(
        {sid for day in days for sid in day["signals"] if str(sid).startswith(prefix)}
    )
    now_s = now.timestamp()
    first = datetime.strptime(min(day["date"] for day in days), "%Y-%m-%d").replace(
        tzinfo=UTC
    )
    window = (first.timestamp(), now_s)
    gap_list = list(gaps)
    outage_list, unreadable = outage_intervals(
        outages, environment=environment, now=now
    )
    union = _union(gap_list + outage_list)

    by_date = {day["date"]: day for day in days}
    total = 0.0
    date = first
    while date.timestamp() < now_s:
        day_start = date.timestamp()
        seconds = _measure(union, day_start, min(day_start + 86400, now_s))
        if seconds > 0:
            total += seconds
            key = date.strftime("%Y-%m-%d")
            day = by_date.setdefault(key, {"date": key, "runs": 0, "signals": {}})
            missed = math.ceil(seconds / check_interval_seconds)
            for sid in signal_ids:
                entry = dict(day["signals"].get(sid) or {"ok": 0, "fail": 0})
                entry["fail"] = _coerce_count(entry.get("fail")) + missed
                entry["lastDomain"] = "unavailable"
                day["signals"][sid] = entry
        date += timedelta(days=1)
    return dict(
        ledger,
        ledger=sorted(by_date.values(), key=lambda day: day["date"], reverse=True),
        unavailability={
            "environment": environment,
            "minutes": round(total / 60, 1),
            "gap_minutes": round(_measure(_union(gap_list), *window) / 60, 1),
            "outage_minutes": round(_measure(_union(outage_list), *window) / 60, 1),
            "outage_count": sum(1 for b, e in outage_list if e > window[0]),
            "unreadable": unreadable,
        },
    )


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _uptime_pct(ok: int, checks: int) -> float:
    """Availability as a percentage; no checks means nothing to disprove (100%)."""
    if checks <= 0:
        return 100.0
    return round((ok / checks) * 100.0, 3)


def build_report_message(
    summary: Mapping[str, Any], *, title: str | None = None
) -> str:
    """Render the positive-proof availability report for Lark."""
    cadence = "daily" if summary.get("window_days") == 1 else "weekly"
    header = title or f"[STABILITY] Infra2 {cadence} availability — positive proof"
    lines = [
        header,
        f"As of {summary['as_of']} | window {summary['window_days']}d | "
        f"{summary['total_runs']} probe runs",
        f"Overall availability: {summary['overall_uptime_pct']}%",
        f"Signals at 100%: {summary['perfect_count']}/{summary['signal_count']}",
    ]
    unavailable = summary.get("unavailability")
    if unavailable:
        line = (
            f"{unavailable['environment']} unavailable {unavailable['minutes']} min, counted "
            f"as failures: {unavailable['gap_minutes']} min with no probe round "
            f"(outage, restart or deploy), {unavailable['outage_minutes']} min unreachable "
            f"per Cloudflare in {unavailable['outage_count']} outage(s)"
        )
        if unavailable.get("unreadable"):
            line += (
                f"; {unavailable['unreadable']} unreadable outage record(s) NOT counted"
            )
        lines.append(line)
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


def build_environment_line(label: str, summary: Mapping[str, Any]) -> str:
    """One line for a secondary environment (staging) in the weekly report."""
    line = (
        f"{label}: {summary['overall_uptime_pct']}% · "
        f"{summary['perfect_count']}/{summary['signal_count']} signals at 100%"
    )
    unavailable = summary.get("unavailability")
    if unavailable:
        line += f" · {unavailable['gap_minutes']} min with no probe round"
    worst = [s for s in summary["signals"] if s["fail"] > 0][:1]
    if worst:
        line += f" · lowest {worst[0]['id']} {worst[0]['uptime_pct']}%"
    return line


def build_stability_card(
    summary: Mapping[str, Any],
    *,
    staging_lines: Iterable[str] = (),
    title: str | None = None,
    signoz_url: str = "https://signoz.zitian.party",
    runbook_url: str = (
        "https://github.com/wangzitian0/infra2/blob/main/docs/ssot/ops.observability.md"
    ),
) -> dict[str, Any]:
    """Build a Feishu interactive card for the positive-proof availability report."""
    cadence = "daily" if summary.get("window_days") == 1 else "weekly"
    degraded = [s for s in summary.get("signals", []) if s.get("fail", 0) > 0][
        :WORST_SIGNALS_SHOWN
    ]
    overall = float(summary.get("overall_uptime_pct", 100.0))
    is_clean = not degraded and overall >= 99.9
    header_title = title or (
        f"{'🟢' if is_clean else '🟡'} [报告] [STABILITY] Infra2 {cadence} availability — positive proof"
    )
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "fields": [
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**Overall Availability**\n{overall}%",
                    },
                },
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            "**Signals at 100%**\n"
                            f"{summary['perfect_count']}/{summary['signal_count']}"
                        ),
                    },
                },
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**Window**\n{summary['window_days']}d (as of {summary['as_of']})",
                    },
                },
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**Probe Runs**\n{summary['total_runs']}",
                    },
                },
            ],
        },
        {"tag": "hr"},
    ]
    unavailable = summary.get("unavailability")
    if isinstance(unavailable, Mapping):
        unavail_text = (
            f"**Unavailability ({unavailable['environment']})**: "
            f"{unavailable['minutes']} min counted as failures "
            f"({unavailable['gap_minutes']} min gap, "
            f"{unavailable['outage_minutes']} min Cloudflare outage in "
            f"{unavailable['outage_count']} incident(s))"
        )
        if unavailable.get("unreadable"):
            unavail_text += (
                f"; {unavailable['unreadable']} unreadable record(s) NOT counted"
            )
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": unavail_text},
            }
        )
    if degraded:
        lines = ["**Lowest availability signals**"]
        for signal in degraded:
            domain = f" `[{signal['last_domain']}]`" if signal.get("last_domain") else ""
            checks = signal["ok"] + signal["fail"]
            lines.append(
                f"• `{signal['id']}`: **{signal['uptime_pct']}%** "
                f"({signal['fail']} fail/{checks} checks){domain}"
            )
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(lines)},
            }
        )
    else:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "✅ **All monitored signals held 100% availability this window.**",
                },
            }
        )
    staging_items = [line for line in staging_lines if line]
    if staging_items:
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "\n".join(f"**Secondary Env**: {line}" for line in staging_items),
                },
            }
        )
    actions = []
    if signoz_url:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "打开 SigNoz"},
                "url": signoz_url,
                "type": "primary",
            }
        )
    if runbook_url:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "查看 Runbook"},
                "url": runbook_url,
                "type": "default",
            }
        )
    if actions:
        elements.append({"tag": "action", "actions": actions})
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "green" if is_clean else "orange",
            "title": {"tag": "plain_text", "content": header_title},
        },
        "elements": elements,
    }


__all__ = [
    "WORST_SIGNALS_SHOWN",
    "apply_unavailability",
    "build_environment_line",
    "build_report_message",
    "build_stability_card",
    "outage_intervals",
    "summarize_ledger",
]
