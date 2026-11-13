"""The "Delivery" section of the weekly and monthly digest (#1645).

``summarize_delivery`` turns stage records (the dicts of ``tools.stage_records list --json``)
into counts for each env (``preview``, ``staging``, ``prod``) and for each change kind (the
prefix of ``units``: ``app``, ``sdk``, ``infra``, ``harness``). A run is the records of one
env that one workflow run wrote. A run passes when it has a ``post`` record and every record
of it passes. ``delivery_lines`` renders the summary as text, and the digest message and the
digest card both use those lines.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

ENVS = ("preview", "staging", "prod")
KINDS = ("app", "sdk", "infra", "harness")
TOP_FAILING = 3
WILSON_Z = 1.96  # two-sided 95%


def wilson_interval(
    passed: int, total: int, z: float = WILSON_Z
) -> tuple[float, float]:
    """The Wilson score interval of a pass rate, as fractions. (0, 0) for no runs."""
    if total <= 0:
        return 0.0, 0.0
    p = passed / total
    centre = (p + z * z / (2 * total)) / (1 + z * z / total)
    half = (
        z
        * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
        / (1 + z * z / total)
    )
    return max(0.0, centre - half), min(1.0, centre + half)


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile, ``q`` in 0..1. 0 for no values."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def _seconds(record: Mapping[str, Any]) -> float:
    started = datetime.fromisoformat(str(record["started_at"]).replace("Z", "+00:00"))
    finished = datetime.fromisoformat(str(record["finished_at"]).replace("Z", "+00:00"))
    return (finished - started).total_seconds()


def _group(runs: list[list[Mapping[str, Any]]]) -> dict[str, Any]:
    passed = sum(
        1
        for run in runs
        if any(r["phase"] == "post" for r in run)
        and all(r["verdict"] == "pass" for r in run)
    )
    failing = Counter(
        c["name"]
        for run in runs
        for r in run
        for c in r["checks"]
        if c["ok"] is not True
    )
    phase = {
        p: [_seconds(r) for run in runs for r in run if r["phase"] == p]
        for p in ("operation", "post")
    }
    low, high = wilson_interval(passed, len(runs))
    return {
        "runs": len(runs),
        "passed": passed,
        "pass_rate": passed / len(runs) if runs else 0.0,
        "wilson_low": low,
        "wilson_high": high,
        "operation_p50": percentile(phase["operation"], 0.5),
        "operation_p90": percentile(phase["operation"], 0.9),
        "post_p50": percentile(phase["post"], 0.5),
        "post_p90": percentile(phase["post"], 0.9),
        "top_failing": sorted(failing.items(), key=lambda item: (-item[1], item[0]))[
            :TOP_FAILING
        ],
    }


def summarize_delivery(
    records: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, dict[str, Any]]]:
    """``{env: {"all": group, kind: group, ...}}`` for the envs that have a run."""
    runs: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for record in records:
        env = str(record["env"]).split(".", 1)[0]
        if env in ENVS:
            runs.setdefault((env, str(record["run_url"])), []).append(record)
    summary: dict[str, dict[str, dict[str, Any]]] = {}
    for env in ENVS:
        env_runs = [run for (name, _), run in runs.items() if name == env]
        if not env_runs:
            continue
        summary[env] = {"all": _group(env_runs)}
        for kind in KINDS:
            kind_runs = [
                run
                for run in env_runs
                if any(
                    u.split(":", 1)[0] == kind for r in run for u in r.get("units", ())
                )
            ]
            if kind_runs:
                summary[env][kind] = _group(kind_runs)
    return summary


def delivery_lines(summary: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> list[str]:
    """One line for each env and each change kind. A line starts with its env or kind."""
    lines: list[str] = []
    for env, groups in summary.items():
        for kind, g in groups.items():
            top = (
                ", ".join(f"{name} x{count}" for name, count in g["top_failing"])
                or "none"
            )
            lines.append(
                f"{env if kind == 'all' else '  ' + kind}: {g['runs']} runs, "
                f"pass {g['pass_rate'] * 100:.1f}% "
                f"(95% CI {g['wilson_low'] * 100:.1f}-{g['wilson_high'] * 100:.1f}), "
                f"operation p50 {g['operation_p50']:.0f} s p90 {g['operation_p90']:.0f} s, "
                f"post p50 {g['post_p50']:.0f} s p90 {g['post_p90']:.0f} s, "
                f"top failing: {top}"
            )
    return lines
