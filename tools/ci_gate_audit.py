#!/usr/bin/env python3
"""Fail-closed drift audit for the infra CI gate inventory (Infra-016 / #461).

Validates ``docs/ssot/ci-gate-inventory.yaml`` against the shared schema and against the
ACTUAL workflows: every gate's ``workflow:job`` must exist (no *dangling* gate), and every
job of a *covered* workflow must be registered (no *unregistered* job).

**What "covered" means is computed, not listed.** A workflow is covered exactly when it can
produce a check on a pull request -- i.e. its own triggers include ``pull_request`` /
``pull_request_target`` (``covered_workflows``). That criterion is the audit's whole reason
to exist: the inventory's consumer is merge authority (``tools/pr_merge_gate.py`` reads
``blocks_merge``), and the hole being guarded is *a job appears on a PR and nobody decided
whether it gates*. A workflow that never runs on a PR cannot open that hole, so registering
it would be bookkeeping with no consumer -- it is reported as ``out_of_scope_workflows``.

This replaces a hand-written ``KNOWN_CI_WORKFLOWS = (infra-ci.yml,)``. A hand-written scope
cannot tell the difference between "deliberately excluded" and "added last Tuesday and
nobody noticed": adding ``pull_request:`` to any workflow used to widen the real risk
surface while leaving the audit's idea of it frozen. Now the scope moves with the triggers.

On the Infra-016 record, precisely: epic #459 closed 2026-06-29 "not planned" for **phases
3-4** (app alignment, cross-repo chain view). Phase 1's own plan -- the archive's D2 line --
scoped "``infra-ci`` 7, ``ops-checks`` 6, reconcile dry-run gate, apply-observability, drift
reports", and shipped only the ``infra-ci`` half; the ``ops.scheduled_cleanup`` stage and the
``reconcile_plan`` / ``preview_leak`` / ``drift_report`` categories were defined for the
other half and sat unused. Registering the PR-triggered remainder finishes Phase 1 as
written; it does not reopen what was closed. The ``blocks_merge: true`` set is unchanged by
that backfill, which is what makes the widening provably non-loosening
(``libs/tests/test_ci_gate_audit.py`` locks the set).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from infra2_sdk.ci import load_delivery_stages, validate_inventory

from tools import ci_spec

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = "docs/ssot/ci-gate-inventory.yaml"
STAGES = "docs/ssot/delivery-stages.yaml"
WORKFLOWS_DIR = ".github/workflows"
# A workflow is in scope exactly when it can put a check on a pull request. Both spellings:
# `pull_request_target` runs on PRs too (with base-repo permissions), so excluding it would
# leave the more privileged of the two unaudited.
PR_TRIGGERS = frozenset({"pull_request", "pull_request_target"})


def _workflow_jobs(path: Path) -> list[str]:
    # `jobs:` as a list or a scalar is valid YAML and an invalid workflow, and
    # `.keys()` on it takes the audit down (#789 review). There is nothing to
    # enumerate, and a crash is not a finding -- the gate surfaces as dangling
    # instead, which is what the caller already knows how to report.
    jobs = ci_spec.load_workflow(path).get("jobs")
    return list(jobs.keys()) if isinstance(jobs, dict) else []


def _workflow_triggers(path: Path) -> set[str]:
    wf = ci_spec.load_workflow(path)
    # PyYAML resolves the bare key `on:` to the boolean True (YAML 1.1 truthy), so a
    # `wf.get("on")`-only read sees nothing on every real workflow file in this repo.
    on = wf.get(True, wf.get("on"))
    if isinstance(on, str):
        return {on}
    if isinstance(on, dict):
        return set(on)
    if isinstance(on, list):
        return {t for t in on if isinstance(t, str)}
    return set()


def _all_workflow_files(root: Path) -> list[str]:
    wf_dir = root / WORKFLOWS_DIR
    if not wf_dir.is_dir():
        return []
    return sorted(
        f"{WORKFLOWS_DIR}/{p.name}"
        for p in list(wf_dir.glob("*.yml")) + list(wf_dir.glob("*.yaml"))
    )


def covered_workflows(root: Path = ROOT) -> tuple[str, ...]:
    """Workflows whose jobs must all be registered: those that run on a pull request."""
    return tuple(
        wf
        for wf in _all_workflow_files(root)
        if _workflow_triggers(root / wf) & PR_TRIGGERS
    )


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
    known = covered_workflows(root)
    result = audit_gates(
        inv.get("gates") or [],
        root=root,
        prefix=inv.get("repo_prefix"),
        known_ci_workflows=known,
    )
    # Not derived from `covered`/gates above -- a reader must see the scope boundary even
    # when the inventory is empty or broken, since that's exactly when "what does this
    # audit even check" matters most.
    result["covered_workflows"] = sorted(known)
    result["out_of_scope_workflows"] = [
        w for w in _all_workflow_files(root) if w not in set(known)
    ]
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CI gate inventory drift audit")
    ap.add_argument(
        "--enforce",
        action="store_true",
        help="exit non-zero on an unregistered job within covered_workflows",
    )
    args = ap.parse_args(argv)
    result = audit()
    print(json.dumps(result, indent=2, ensure_ascii=False))

    hard = bool(result["schema_errors"] or result["dangling_gates"])
    if hard:
        print(
            "::error::schema errors / dangling gates are not allowed even in shadow",
            file=sys.stderr,
        )
        return 1
    if args.enforce and result["unregistered_jobs"]:
        # Scoped on purpose (#780 review): an unqualified "every job must be
        # coordinate-ized" would tell a reader the audit expects out_of_scope_workflows
        # to be registered too -- exactly the overclaim that review caught. State the
        # criterion instead, so a reader can check their own workflow against it.
        print(
            "::error::unregistered CI jobs within covered_workflows "
            f"{result['covered_workflows']} must be registered in {INVENTORY} -- "
            "a workflow is covered exactly when it runs on a pull request "
            f"({sorted(PR_TRIGGERS)}); workflows in out_of_scope_workflows are not "
            "backlog, they never run on a pull request so they cannot gate one: "
            f"{result['unregistered_jobs']}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
