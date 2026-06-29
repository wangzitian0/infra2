#!/usr/bin/env python3
"""Fail-closed drift audit for the infra CI gate inventory (Infra-016 / #461).

Validates ``docs/ssot/ci-gate-inventory.yaml`` against the shared schema and against the
ACTUAL workflows: every gate's ``workflow:job`` must exist (no *dangling* gate), and every
job of a fully-covered CI workflow must be registered (no *unregistered* job).

Ratchet: schema errors and dangling gates are ALWAYS hard (even in shadow). Unregistered
jobs are the Phase-1 backlog — reported but non-fatal until ``--enforce`` (Phase 2), so the
audit can ship report-only in CI first and flip to blocking once coverage is complete.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from libs.ci_gate_schema import load_delivery_stages, validate_inventory

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = "docs/ssot/ci-gate-inventory.yaml"
STAGES = "docs/ssot/delivery-stages.yaml"
# Workflows whose jobs must (eventually) ALL be registered. Phase 1: only infra-ci is
# fully covered; extend this list as ops-checks / reconcile / etc. get coordinate-ized.
KNOWN_CI_WORKFLOWS = (".github/workflows/infra-ci.yml",)


def _workflow_jobs(path: Path) -> list[str]:
    # Non-file (missing, or a bare/empty `workflow:` resolving to the repo root dir) → no
    # jobs, so the gate surfaces as dangling/schema_error and the audit exits 1 cleanly
    # rather than crashing with IsADirectoryError.
    if not path.is_file():
        return []
    wf = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list((wf.get("jobs") or {}).keys())


def audit_gates(
    gates: list[dict], *, root: Path, prefix: str | None, known_ci_workflows
) -> dict:
    stage_ids = set(load_delivery_stages(root / STAGES))
    schema = validate_inventory(gates, stage_ids=stage_ids, id_prefix=prefix)

    covered: dict[str, set[str]] = {}
    dangling: list[str] = []
    for gate in gates:
        wf, job = gate.get("workflow", ""), gate.get("job", "")
        if job and job not in _workflow_jobs(root / wf):
            dangling.append(f"{gate.get('id', '?')} -> {wf}:{job}")
        covered.setdefault(wf, set()).add(job)

    unregistered: list[str] = []
    for wf in known_ci_workflows:
        for job in _workflow_jobs(root / wf):
            if job not in covered.get(wf, set()):
                unregistered.append(f"{wf}:{job}")

    return {
        "schema_errors": schema["errors"],
        "dangling_gates": sorted(dangling),
        "unregistered_jobs": sorted(unregistered),
    }


def audit(root: Path = ROOT) -> dict:
    inv = yaml.safe_load((root / INVENTORY).read_text(encoding="utf-8")) or {}
    return audit_gates(
        inv.get("gates") or [],
        root=root,
        prefix=inv.get("repo_prefix"),
        known_ci_workflows=KNOWN_CI_WORKFLOWS,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CI gate inventory drift audit")
    ap.add_argument("--enforce", action="store_true", help="exit non-zero on ANY drift (Phase 2)")
    args = ap.parse_args(argv)
    result = audit()
    print(json.dumps(result, indent=2, ensure_ascii=False))

    hard = bool(result["schema_errors"] or result["dangling_gates"])
    if hard:
        print("::error::schema errors / dangling gates are not allowed even in shadow", file=sys.stderr)
        return 1
    if args.enforce and result["unregistered_jobs"]:
        print("::error::unregistered CI jobs (every job must be coordinate-ized)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
