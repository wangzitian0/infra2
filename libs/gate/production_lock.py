"""The production lock: the GitHub environment that stops a production job (#1035, #1138).

The lock holds when four facts are true. The `production` environment has a required
reviewer. Administrators cannot bypass it. It accepts deployments only through custom
branch and tag policies. Those policies are exactly branch `main` and tag `v*`.
A fact that cannot be read counts as false.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from libs.gate.types import Runner

ENVIRONMENT = "production"
REQUIRED_POLICIES = frozenset({("main", "branch"), ("v*", "tag")})
REVIEWER_TYPES = frozenset({"User", "Team"})


@dataclass(frozen=True)
class LockFacts:
    reviewers: int = 0
    can_admins_bypass: bool | None = None
    custom_policies: bool = False
    policies: frozenset[tuple[str, str]] = frozenset()
    error: str = ""


class _Unreadable(Exception):
    """A response that does not have the documented shape."""


def _read_json(gh: Runner, path: str) -> object:
    try:
        return json.loads(gh(["api", path]))
    except json.JSONDecodeError as exc:
        raise _Unreadable(f"{path} did not return JSON") from exc


def _reviewer_count(rules: object) -> int:
    if not isinstance(rules, list):
        raise _Unreadable("protection_rules is not a list")
    count = 0
    for rule in rules:
        if not isinstance(rule, dict) or "type" not in rule:
            raise _Unreadable("a protection rule has no type")
        if rule["type"] != "required_reviewers":
            continue
        reviewers = rule.get("reviewers")
        if not isinstance(reviewers, list):
            raise _Unreadable("a required_reviewers rule has no reviewers list")
        count += sum(
            1
            for reviewer in reviewers
            if isinstance(reviewer, dict) and reviewer.get("type") in REVIEWER_TYPES
        )
    return count


def _policies(payload: object) -> frozenset[tuple[str, str]]:
    if not isinstance(payload, dict) or not isinstance(
        payload.get("branch_policies"), list
    ):
        raise _Unreadable("deployment-branch-policies has no branch_policies list")
    entries = payload["branch_policies"]
    if payload.get("total_count") != len(entries):
        raise _Unreadable("deployment-branch-policies returned a partial list")
    policies: set[tuple[str, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict) or "name" not in entry or "type" not in entry:
            raise _Unreadable("a deployment policy has no name or type")
        policies.add((str(entry["name"]), str(entry["type"])))
    return frozenset(policies)


def read_lock_facts(repo: str, *, gh: Runner | None = None) -> LockFacts:
    """Read the lock facts through `gh`. A failure returns `LockFacts(error=...)`."""
    if gh is None:
        from libs.gate.client import _gh

        gh = _gh
    base = f"repos/{repo}/environments/{ENVIRONMENT}"
    try:
        environment = _read_json(gh, base)
        if not isinstance(environment, dict):
            raise _Unreadable("the environment is not a JSON object")
        for key in (
            "can_admins_bypass",
            "protection_rules",
            "deployment_branch_policy",
        ):
            if key not in environment:
                raise _Unreadable(f"the environment has no {key}")
        bypass = environment["can_admins_bypass"]
        branch_policy = environment["deployment_branch_policy"]
        if branch_policy is not None and not isinstance(branch_policy, dict):
            raise _Unreadable("deployment_branch_policy is not an object")
        custom = (
            bool(branch_policy) and branch_policy.get("custom_branch_policies") is True
        )
        policies = (
            _policies(_read_json(gh, f"{base}/deployment-branch-policies?per_page=100"))
            if custom
            else frozenset()
        )
        return LockFacts(
            reviewers=_reviewer_count(environment["protection_rules"]),
            can_admins_bypass=bypass if isinstance(bypass, bool) else None,
            custom_policies=custom,
            policies=policies,
        )
    except Exception as exc:  # noqa: BLE001 - any failure leaves the lock unproven
        return LockFacts(error=f"{type(exc).__name__}: {exc}"[:200])


def lock_failures(facts: LockFacts) -> list[str]:
    """One reason per lock fact that is false. An empty list means the lock holds."""
    if facts.error:
        return [f"the {ENVIRONMENT} environment could not be read ({facts.error})"]
    failures: list[str] = []
    if facts.reviewers < 1:
        failures.append(f"the {ENVIRONMENT} environment has no required reviewer")
    if facts.can_admins_bypass is not False:
        failures.append(
            f"administrators can bypass the {ENVIRONMENT} environment "
            f"(can_admins_bypass={facts.can_admins_bypass!r})"
        )
    if not facts.custom_policies:
        failures.append(
            f"the {ENVIRONMENT} environment does not limit deployments to custom "
            "branch and tag policies"
        )
    for name, kind in sorted(REQUIRED_POLICIES - facts.policies):
        failures.append(
            f"the {ENVIRONMENT} environment has no deployment policy for {kind} {name!r}"
        )
    for name, kind in sorted(facts.policies - REQUIRED_POLICIES):
        failures.append(
            f"the {ENVIRONMENT} environment also accepts deployments from {kind} {name!r}"
        )
    return failures
