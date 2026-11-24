#!/usr/bin/env python3
"""Report the hooks whose p95 duration passes 70% of their budget (#1823).

The truealpha `staging_gate` took 117 s, then 289 s, then more than 360 s, and nothing warned
before it timed out. Each stage record holds `checks[].seconds`. This tool reads the newest
stage records through the public `list_records` of `common/deploy/stage_record_reader.py`,
groups the seconds by hook and prints one line per hook with the p95, the budget and the ratio.

Report only: a hook over the threshold gives a `::warning::` annotation and a line in the step
summary. The exit code is 0 whatever the durations are. It is 1 only when the records cannot be
read, so that a broken report does not look like a quiet one.

The budget of a hook is its entry in `HOOK_BUDGET_SECONDS`, else the cell budget of
`infra2_sdk.manifests.CELL_POLICIES` for the env and phase of its record.

    GITHUB_TOKEN=... GITHUB_REPOSITORY=owner/repo python tools/hook_duration_report.py
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.deploy.stage_record_reader import list_records  # noqa: E402

#: A hook is reported when its p95 is above this share of its budget.
THRESHOLD = 0.70
#: Fewer samples than this give no p95 that means anything.
MIN_SAMPLES = 5
#: The newest N subjects (tags or commits) of each env that the report reads.
LAST_SUBJECTS = 20
#: The envs whose records the report reads (prefix of a stage id, see `list_records`).
ENVS = ("staging", "prod")

#: Effective budget of a hook that is smaller than its cell budget. `infra2_sdk.manifests`
#: gives a cell the smaller of the cell budget and the sum of its check timeouts. The truealpha
#: `staging_gate` sums 90 + 30 + 240 = 360 s, and the cell `staging.post` allows 600 s: a
#: gate that takes 365 s fails while 365 s is only 61% of the cell budget. Change an entry when
#: the check timeouts of that hook manifest change.
HOOK_BUDGET_SECONDS: dict[str, int] = {
    "truealpha/app:staging_gate": 360,
}


def cell_budgets() -> dict[tuple[str, str], int]:
    """Budget in seconds per (stage id, phase), from the policy of the pinned SDK."""
    from infra2_sdk.manifests import CELL_POLICIES

    budgets: dict[tuple[str, str], int] = {}
    for policy in CELL_POLICIES.values():
        key = (policy.stage_id, policy.phase)
        budgets[key] = min(
            budgets.get(key, policy.budget_seconds), policy.budget_seconds
        )
    return budgets


def percentile(values: Iterable[float], share: float) -> float:
    """Nearest-rank percentile: the smallest value with at least `share` of the values below."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("no values")
    return ordered[max(math.ceil(share * len(ordered)) - 1, 0)]


def summarize(
    records: Iterable[Mapping[str, Any]],
    budgets: Mapping[tuple[str, str], int],
    hook_budgets: Mapping[str, int] = HOOK_BUDGET_SECONDS,
) -> list[dict[str, Any]]:
    """One row per (env, phase, check): sample count, p95 seconds, budget, ratio, flag."""
    samples: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for record in records:
        for check in record.get("checks") or ():
            seconds = check.get("seconds")
            if (
                check.get("skipped")
                or not isinstance(seconds, (int, float))
                or seconds <= 0
            ):
                continue
            samples[(record["env"], record["phase"], check["name"])].append(
                float(seconds)
            )
    rows = []
    for (env, phase, name), values in sorted(samples.items()):
        budget = hook_budgets.get(name) or budgets.get((env, phase))
        if budget is None:
            continue
        enough = len(values) >= MIN_SAMPLES
        p95 = percentile(values, 0.95)
        rows.append(
            {
                "env": env,
                "phase": phase,
                "check": name,
                "samples": len(values),
                "p95": p95,
                "budget": budget,
                "ratio": p95 / budget,
                "enough": enough,
                "flagged": enough and p95 > THRESHOLD * budget,
            }
        )
    return rows


def render(rows: list[dict[str, Any]]) -> str:
    lines = [
        f"Hook duration report: p95 of the newest {LAST_SUBJECTS} subjects per env, "
        f"threshold {THRESHOLD:.0%} of budget",
        "",
        "| env | phase | check | samples | p95 s | budget s | share | state |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        state = (
            "OVER THRESHOLD"
            if r["flagged"]
            else "ok"
            if r["enough"]
            else f"fewer than {MIN_SAMPLES} samples"
        )
        lines.append(
            f"| {r['env']} | {r['phase']} | {r['check']} | {r['samples']} | "
            f"{r['p95']:.0f} | {r['budget']} | {r['ratio']:.0%} | {state} |"
        )
    return "\n".join(lines)


def annotations(rows: list[dict[str, Any]]) -> list[str]:
    return [
        f"::warning title=Hook duration::{r['check']} ({r['env']} {r['phase']}) "
        f"p95 {r['p95']:.0f}s is {r['ratio']:.0%} of its {r['budget']}s budget "
        f"(threshold {THRESHOLD:.0%}, {r['samples']} samples)"
        for r in rows
        if r["flagged"]
    ]


def main(argv: list[str] | None = None, list_records_fn=list_records) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    args = parser.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN", "")
    if not args.repo or not token:
        print(
            "::error::GITHUB_REPOSITORY and GITHUB_TOKEN are required", file=sys.stderr
        )
        return 1
    records: list[dict] = []
    try:
        for env in ENVS:
            records += list_records_fn(args.repo, token, env=env, last=LAST_SUBJECTS)
    except Exception as exc:  # noqa: BLE001 - an unreadable record set must not read as quiet
        print(f"::error::cannot read the stage records: {type(exc).__name__}: {exc}")
        return 1
    if not records:
        print("::error::no stage record was read; the report would be empty")
        return 1
    rows = summarize(records, cell_budgets())
    report = render(rows)
    print(report)
    for line in annotations(rows):
        print(line)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(report + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
