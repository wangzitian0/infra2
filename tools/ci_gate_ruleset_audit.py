#!/usr/bin/env python3
"""Fail-closed drift audit: docs/ssot/ci-gate-inventory.yaml's `blocks_merge: true`
gates vs the LIVE GitHub branch ruleset for `main` (#504).

The inventory is a declaration; nothing previously checked it was actually enforced.
main's ruleset had zero `required_status_checks` until #504 — every `blocks_merge: true`
gate was decorative. This mirrors tools/watchdog_consistency_audit.py's pattern (a real
signal source vs an inventory) rather than trusting the YAML on its own.

Also flags the inverse smell: a gate declared `blocks_merge: true` whose job is
`continue-on-error: true` can never actually fail the run, so it can never block
merge no matter what the ruleset says (see infra_ci.vault_policy, which is why this
audit exists).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

import yaml

from tools import ci_spec

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs/ssot/ci-gate-inventory.yaml"
DEFAULT_REPOSITORY = "wangzitian0/infra2"
DEFAULT_BRANCH = "main"


def _blocking_gates() -> list[dict]:
    inv = yaml.safe_load(INVENTORY.read_text(encoding="utf-8")) or {}
    return [g for g in (inv.get("gates") or []) if g.get("blocks_merge")]


def _job_display_name(workflow_rel_path: str, job_id: str) -> str | None:
    """The GitHub Actions check `context` a job reports is its `name:`, not its
    YAML job id — required_status_checks matches on the display name."""
    workflow = ci_spec.load_workflow(ROOT / workflow_rel_path)
    jobs = workflow.get("jobs")
    job = (jobs if isinstance(jobs, dict) else {}).get(job_id)
    return job.get("name") if isinstance(job, dict) else None


def _defanged(workflow_rel_path: str, job_id: str) -> list[str]:
    """Parts of a required gate that can no longer fail it.

    This used to read only ``job.get("continue-on-error")``. One level down
    was invisible: a `continue-on-error` on the step that actually runs
    `ruff`, or the tests, leaves the job's name, its `if:` and its `needs:`
    byte-identical while the required check becomes unconditionally green --
    measured, and all three gate auditors passed it.

    The reading now lives in ``tools.ci_spec`` so there is one place to fix,
    which is why this was missed for so long: the same question was being
    answered in three files and deepened in none.
    """
    workflow, source = ci_spec.read_workflow(ROOT / workflow_rel_path)
    return ci_spec.defanged_steps(workflow, job_id, source)


def _live_required_contexts(
    repository: str, branch: str, token: str, *, opener=urllib.request.urlopen
) -> tuple[set[str], set[int]] | None:
    """What GitHub enforces for `branch`: the required status check contexts, and
    the ids of the rulesets they came from.

    None if it could not be determined. Fail-safe: a caller must not read None as
    "no requirements" -- that is the same shape of hole as an empty protected set.

    The ruleset ids ride along because `_ruleset_posture` needs them and this is
    the call that already knows them; asking twice would let the two answers
    describe different rulesets.
    """
    url = f"https://api.github.com/repos/{repository}/rules/branches/{branch}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "infra2-ci-gate-ruleset-audit/1.0",
        },
    )
    try:
        with opener(request, timeout=20) as response:
            rules = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - any failure -> undetermined, fail safe
        return None
    contexts: set[str] = set()
    ruleset_ids: set[int] = set()
    for rule in rules if isinstance(rules, list) else []:
        rid = rule.get("ruleset_id")
        if isinstance(rid, int):
            ruleset_ids.add(rid)
        if rule.get("type") != "required_status_checks":
            continue
        for check in rule.get("parameters", {}).get("required_status_checks", []):
            if isinstance(check, dict) and check.get("context"):
                contexts.add(check["context"])
    return contexts, ruleset_ids


def _ruleset_posture(
    repository: str, ruleset_ids: set[int], token: str, opener=urllib.request.urlopen
) -> dict | None:
    """Whether the rulesets carrying those rules can actually stop anyone.

    Two fields decide that, and neither appears in the branch-rules response
    the required-check comparison reads:

    - ``enforcement``: a ruleset set to ``disabled`` or ``evaluate`` keeps every
      rule listed and stops applying them.
    - ``bypass_actors``: anyone listed merges past every required check.

    Both matter more than the check list they qualify, and both are editable
    from the GitHub UI, which leaves **no trace in this repository at all** --
    no commit, no diff, nothing a reviewer could see. This audit is the only
    thing positioned to notice, and until now it read neither.
    """
    posture: dict = {"rulesets": [], "inactive": [], "bypassable": []}
    for rid in sorted(ruleset_ids):
        request = urllib.request.Request(
            f"https://api.github.com/repos/{repository}/rulesets/{rid}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "infra2-ci-gate-ruleset-audit/1.0",
            },
        )
        try:
            with opener(request, timeout=20) as response:
                body = json.loads(response.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 - unreadable posture is undetermined
            return None
        name = str(body.get("name") or rid)
        enforcement = str(body.get("enforcement") or "")
        actors = body.get("bypass_actors") or []
        posture["rulesets"].append(
            {"id": rid, "name": name, "enforcement": enforcement}
        )
        if enforcement != "active":
            posture["inactive"].append(
                f"{name}: enforcement={enforcement or 'unknown'}"
            )
        if actors:
            posture["bypassable"].append(f"{name}: {len(actors)} bypass actor(s)")
    return posture


def audit(
    *, repository: str = DEFAULT_REPOSITORY, branch: str = DEFAULT_BRANCH, token: str
) -> dict:
    gates = _blocking_gates()

    declared: dict[str, str] = {}  # display name -> gate id
    self_contradicting: list[str] = []
    unresolvable: list[str] = []
    for gate in gates:
        name = _job_display_name(gate.get("workflow", ""), gate.get("job", ""))
        if name is None:
            unresolvable.append(gate.get("id", "?"))
            continue
        declared[name] = gate.get("id", "?")
        defanged = _defanged(gate.get("workflow", ""), gate.get("job", ""))
        if defanged:
            self_contradicting.append(f"{gate.get('id', '?')} ({', '.join(defanged)})")

    live_pair = _live_required_contexts(repository, branch, token)
    live, ruleset_ids = (None, set()) if live_pair is None else live_pair

    result: dict = {
        "declared_blocking_checks": sorted(declared),
        "self_contradicting_gates": sorted(self_contradicting),
        "unresolvable_gates": sorted(unresolvable),
    }
    if live is None:
        result["live_required_checks"] = None
        result["status"] = "undetermined (could not reach GitHub rules API)"
        return result

    posture = _ruleset_posture(repository, ruleset_ids, token)
    if posture is None:
        result["live_required_checks"] = sorted(live)
        result["status"] = "undetermined (could not read ruleset enforcement)"
        return result
    result["rulesets"] = posture["rulesets"]
    result["inactive_rulesets"] = posture["inactive"]
    result["bypassable_rulesets"] = posture["bypassable"]

    missing_from_ruleset = sorted(set(declared) - live)
    extra_in_ruleset = sorted(live - set(declared))
    result["live_required_checks"] = sorted(live)
    result["missing_from_ruleset"] = missing_from_ruleset
    result["extra_in_ruleset"] = extra_in_ruleset
    result["status"] = (
        "drift"
        if (
            missing_from_ruleset
            or extra_in_ruleset
            or self_contradicting
            or unresolvable
            # A ruleset that is not enforcing, or that anyone can bypass, makes
            # the required-check list above decorative -- a larger drift than
            # any disagreement inside it.
            or posture["inactive"]
            or posture["bypassable"]
        )
        else "in_sync"
    )
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--repository", default=os.getenv("GITHUB_REPOSITORY", DEFAULT_REPOSITORY)
    )
    ap.add_argument("--branch", default=DEFAULT_BRANCH)
    ap.add_argument(
        "--token",
        default=os.getenv("GITHUB_TOKEN") or os.getenv("GH_PAT"),
        help="GitHub token to read the branch ruleset (default: env GITHUB_TOKEN / GH_PAT). "
        "Absent -> drift undetermined, fail-safe (not treated as in_sync).",
    )
    ap.add_argument(
        "--enforce",
        action="store_true",
        help="exit non-zero on drift or an undetermined check (default: report-only)",
    )
    args = ap.parse_args(argv)

    if not args.token:
        print(
            "no GitHub token available — cannot verify the live ruleset; "
            "declared blocking gates only:",
            file=sys.stderr,
        )
        result = {
            "declared_blocking_checks": sorted(
                {
                    n
                    for g in _blocking_gates()
                    if (n := _job_display_name(g.get("workflow", ""), g.get("job", "")))
                }
            ),
            "live_required_checks": None,
            "status": "undetermined (no token)",
        }
    else:
        result = audit(repository=args.repository, branch=args.branch, token=args.token)

    print(json.dumps(result, indent=2, ensure_ascii=False))

    if args.enforce and result["status"] != "in_sync":
        print(
            "::error::ci-gate-inventory.yaml is out of sync with the live ruleset",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
