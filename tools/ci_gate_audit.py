#!/usr/bin/env python3
"""Fail-closed drift audit for the infra CI gate inventory (Infra-016 / #461).

Validates ``docs/ssot/ci-gate-inventory.yaml`` against the shared schema and against the
ACTUAL workflows: every gate's ``workflow:job`` must exist (no *dangling* gate), and every
job of a *covered* CI workflow (``KNOWN_CI_WORKFLOWS``) must be registered (no
*unregistered* job).

Ratchet: schema errors and dangling gates are ALWAYS hard, everywhere. Unregistered jobs
are hard only inside the covered scope, and only with ``--enforce`` — ``infra-ci.yml`` is
covered and currently has zero unregistered jobs (``libs/tests/test_ci_gate_audit.py``
locks that), so ``infra-ci.yml`` itself now runs this audit with ``--enforce``: a job added
there without a matching gate fails CI, not just a printed backlog line.

Coverage is deliberately narrow, and staying narrow is a *decision*, not a gap someone
forgot to close. Infra-016's own plan (docs/project/archive/Infra-016.ci_gate_inventory.md)
scoped a Phase 2 that would have coordinate-ized ops-checks/reconcile-iac-inputs/
apply-observability/etc. too — but the epic (#459) closed 2026-06-29 as "not planned": the
coordinate-ize-every-workflow approach converged into boundary governance instead, and
#460/#461 (the infra side, i.e. exactly this file + inventory) were explicitly "completed
and kept frozen", not left as an active backlog. Widening ``KNOWN_CI_WORKFLOWS`` reopens
that closed epic decision — it is not a routine backfill, so it isn't done here without
revisiting that record. What a reader of a report *does* need, and what silently scoping to
one workflow denied them before, is to see the boundary without reading this comment or the
archive: ``audit()``'s ``covered_workflows`` / ``out_of_scope_workflows`` fields name it in
the machine-readable output itself.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from infra2_sdk.ci import load_delivery_stages, validate_inventory

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = "docs/ssot/ci-gate-inventory.yaml"
STAGES = "docs/ssot/delivery-stages.yaml"
WORKFLOWS_DIR = ".github/workflows"
# Workflows whose jobs must ALL be registered. Scope is frozen at infra-ci.yml — see the
# module docstring for why this is not "Phase 1 of 2, pending" despite older comments/
# commit messages saying so; expanding it means re-opening Infra-016 (#459), not filling
# in a backlog.
KNOWN_CI_WORKFLOWS = (f"{WORKFLOWS_DIR}/infra-ci.yml",)


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


def _all_workflow_files(root: Path) -> list[str]:
    wf_dir = root / WORKFLOWS_DIR
    if not wf_dir.is_dir():
        return []
    return sorted(
        f"{WORKFLOWS_DIR}/{p.name}"
        for p in list(wf_dir.glob("*.yml")) + list(wf_dir.glob("*.yaml"))
    )


def audit(root: Path = ROOT) -> dict:
    inv = yaml.safe_load((root / INVENTORY).read_text(encoding="utf-8")) or {}
    result = audit_gates(
        inv.get("gates") or [],
        root=root,
        prefix=inv.get("repo_prefix"),
        known_ci_workflows=KNOWN_CI_WORKFLOWS,
    )
    # Not derived from `covered`/gates above -- a reader must see the scope boundary even
    # when the inventory is empty or broken, since that's exactly when "what does this
    # audit even check" matters most.
    known = set(KNOWN_CI_WORKFLOWS)
    result["covered_workflows"] = sorted(known)
    result["out_of_scope_workflows"] = [
        w for w in _all_workflow_files(root) if w not in known
    ]
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CI gate inventory drift audit")
    ap.add_argument(
        "--enforce",
        action="store_true",
        help="exit non-zero on an unregistered job within covered_workflows (KNOWN_CI_WORKFLOWS)",
    )
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
