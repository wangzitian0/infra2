"""Report the hooks whose p95 duration passes 70% of their budget (#1823)."""

from __future__ import annotations

import argparse
import math
import os
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

from common.core.constants import APP_SOURCES
from common.deploy.app_contract import _SLOT_CELLS, fetch_hooks_manifest
from common.deploy.stage_record_reader import list_records
from common.deploy.yaml_text import safe_load_text

ROOT = Path(__file__).resolve().parents[2]
#: A hook is reported when its p95 is above this share of its budget.
THRESHOLD = 0.70
#: Fewer samples than this give no p95 that means anything.
MIN_SAMPLES = 5
#: The newest N subjects (tags or commits) of each env that the report reads.
LAST_SUBJECTS = 20
#: The envs whose records the report reads (prefix of a stage id, see `list_records`).
ENVS = ("staging", "prod")
REPOS_YAML = ROOT / "harness/repos.yaml"


def hook_budget_from_manifest(manifest: Any, cell_id: str) -> int | None:
    """Return effective budget seconds for ``cell_id`` in ``manifest``.

    Equals the smaller of ``CELL_POLICIES[cell_id].budget_seconds`` and the sum of
    active check ``timeout_seconds`` in that cell, or ``None`` when no active check runs.
    """
    from infra2_sdk.delivery import CELL_POLICIES, resolve_check_cells

    policy = CELL_POLICIES.get(cell_id)
    if policy is None:
        return None
    active = [
        check for check in manifest.checks if cell_id in resolve_check_cells(check)
    ]
    if not active:
        return None
    timeout_sum = sum(check.timeout_seconds for check in active)
    return (
        min(timeout_sum, policy.budget_seconds)
        if timeout_sum > 0
        else policy.budget_seconds
    )


def hook_budgets_from_manifests(manifests: Mapping[str, Any]) -> dict[str, int]:
    """Derive ``{service_id:slot -> seconds}`` budgets from loaded hook manifests."""
    budgets: dict[str, int] = {}
    for service_id, manifest in sorted(manifests.items()):
        for slot, cell_ids in _SLOT_CELLS.items():
            cell_budgets_for_slot = [
                b
                for cell_id in cell_ids
                if (b := hook_budget_from_manifest(manifest, cell_id)) is not None
            ]
            if cell_budgets_for_slot:
                budgets[f"{service_id}:{slot}"] = min(cell_budgets_for_slot)
    return budgets


def load_hook_budgets(
    repo_root: Path = ROOT,
    manifest_fetcher: Callable[[str], bytes | str] | None = None,
) -> dict[str, int]:
    """Load local and pinned App hook manifests and derive hook budgets without overrides."""
    from common.deploy.stage_hooks import load_hooks_manifest

    manifests: dict[str, Any] = {}
    todo_manifest = repo_root / "platform/30.todo/runtime-hooks.manifest.json"
    if todo_manifest.is_file():
        manifests["platform/todo"] = load_hooks_manifest(todo_manifest)

    repos_yaml = repo_root / "harness/repos.yaml"
    tags_by_slug: dict[str, str] = {}
    if repos_yaml.is_file():
        data = safe_load_text(yaml, repos_yaml.read_text(encoding="utf-8"))
        for entry in data.get("repositories") or ():
            source = str(entry.get("source") or "")
            tag = str(entry.get("release_tag") or "").strip()
            slug = source.removeprefix("https://github.com/").removesuffix(".git")
            if slug and tag:
                tags_by_slug[slug] = tag

    for service_id, slug in sorted(APP_SOURCES.items()):
        tag = tags_by_slug.get(slug)
        if not tag:
            continue
        try:
            manifests[service_id] = fetch_hooks_manifest(
                service_id,
                source_sha=tag,
                manifest_fetcher=manifest_fetcher,
            )
        except Exception as exc:  # noqa: BLE001 - fall back to cell budget when offline
            print(
                f"::warning title=Hook duration::cannot load hooks manifest for {service_id}@{tag}: {exc}",
                file=sys.stderr,
            )
    return hook_budgets_from_manifests(manifests)


def cell_budgets() -> dict[tuple[str, str], int]:
    """Budget in seconds per (stage id, phase), from the policy of the pinned SDK."""
    from infra2_sdk.delivery import CELL_POLICIES

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
    hook_budgets: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    """One row per (env, phase, check): sample count, p95 seconds, budget, ratio, flag."""
    resolved_hook_budgets = hook_budgets or {}
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
        budget = resolved_hook_budgets.get(name) or budgets.get((env, phase))
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


def main(
    argv: list[str] | None = None,
    list_records_fn=list_records,
    hook_budgets_fn: Callable[[], dict[str, int]] = load_hook_budgets,
) -> int:
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
    rows = summarize(records, cell_budgets(), hook_budgets=hook_budgets_fn())
    report = render(rows)
    print(report)
    for line in annotations(rows):
        print(line)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(report + "\n")
    return 0
