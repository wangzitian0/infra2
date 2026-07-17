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
    path = ROOT / workflow_rel_path
    if not path.is_file():
        return None
    wf = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    job = (wf.get("jobs") or {}).get(job_id) or {}
    return job.get("name")


def _job_continue_on_error(workflow_rel_path: str, job_id: str) -> bool:
    path = ROOT / workflow_rel_path
    if not path.is_file():
        return False
    wf = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    job = (wf.get("jobs") or {}).get(job_id) or {}
    return bool(job.get("continue-on-error"))


def _live_required_contexts(
    repository: str, branch: str, token: str, *, opener=urllib.request.urlopen
) -> set[str] | None:
    """Required status check contexts GitHub actually enforces for `branch`, or
    None if it couldn't be determined (fail-safe: caller must not treat None as
    "no requirements")."""
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
    for rule in rules if isinstance(rules, list) else []:
        if rule.get("type") != "required_status_checks":
            continue
        for check in rule.get("parameters", {}).get("required_status_checks", []):
            if isinstance(check, dict) and check.get("context"):
                contexts.add(check["context"])
    return contexts


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
        if _job_continue_on_error(gate.get("workflow", ""), gate.get("job", "")):
            self_contradicting.append(gate.get("id", "?"))

    live = _live_required_contexts(repository, branch, token)

    result: dict = {
        "declared_blocking_checks": sorted(declared),
        "self_contradicting_gates": sorted(self_contradicting),
        "unresolvable_gates": sorted(unresolvable),
    }
    if live is None:
        result["live_required_checks"] = None
        result["status"] = "undetermined (could not reach GitHub rules API)"
        return result

    missing_from_ruleset = sorted(set(declared) - live)
    extra_in_ruleset = sorted(live - set(declared))
    result["live_required_checks"] = sorted(live)
    result["missing_from_ruleset"] = missing_from_ruleset
    result["extra_in_ruleset"] = extra_in_ruleset
    result["status"] = (
        "drift"
        if (missing_from_ruleset or extra_in_ruleset or self_contradicting or unresolvable)
        else "in_sync"
    )
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repository", default=os.getenv("GITHUB_REPOSITORY", DEFAULT_REPOSITORY))
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
        print("::error::ci-gate-inventory.yaml is out of sync with the live ruleset", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
