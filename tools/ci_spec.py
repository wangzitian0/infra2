"""Single source of truth for cross-repo CI testing hierarchy and compute budgets.

Defines the Left-to-Right Testing Hierarchy (L0 Gate -> L1 Integrate -> L2 Heavy -> L3 Release):
- L0 PR Gate: Total wall clock <= 90s, individual test <= 30s, shards between 2 and 8.
- L2 Heavy / Nightly: Benchmarks, full reconciliation, and soak testing.
"""

from __future__ import annotations

SPEC_VERSION = "1.0.0"

# L0 Gate Budgets
GATE_WALL_CLOCK_BUDGET_S = 90  # Hard ceiling for PR gate wall-clock runtime
SOFT_WARN_S = 75  # Soft warning line for PR gate
MAX_SINGLE_TEST_S = 30  # Single tests exceeding this budget are forbidden in L0
TARGET_PER_SHARD_S = 45  # Optimal target duration per shard
SHARD_MIN = 2  # Minimum shards when sharding is activated
SHARD_MAX = 8  # Hard upper limit on shards to prevent VM cold-start fragmentation

# Forbidden test patterns in PR / merge-queue gates (must migrate to L2 nightly)
BANNED_IN_GATE_PATTERNS = (
    "benchmark",
    "make bench",
    "reconciliation",
    "full_recon",
    "full_reconcile",
    "soak_test",
)

# Markers forbidden in L0 gate pytest runs
BANNED_GATE_MARKERS = (
    "benchmark",
    "slow",
    "reconciliation",
    "full_recon",
)


# ---------------------------------------------------------------------------
# One reading of a workflow, shared by every gate auditor.
#
# There were three: ci_gate_audit, ci_gate_ruleset_audit and ci_gate_lint each
# parsed workflow YAML themselves -- six `yaml.safe_load` calls across three
# files, for the same files, answering overlapping questions. That is why a
# gate could be defanged and all three miss it: the one check that looks for
# `continue-on-error` lived in one of them and only looked at the job, so
# closing it at step level meant finding and fixing three places, and nobody
# did. Asking the question once means answering it once.
# ---------------------------------------------------------------------------

import re  # noqa: E402
import yaml  # noqa: E402  (kept next to the helpers that use it)


def load_workflow(path) -> dict:
    """A workflow's parsed YAML, or {} when it is absent or unreadable.

    Returning {} rather than raising: an auditor's job is to report, and a
    workflow that has been deleted is a finding for the caller to phrase, not
    a traceback.
    """
    from pathlib import Path

    p = Path(path)
    if not p.is_file():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


# A `continue-on-error` is not automatically wrong -- uploading coverage to a
# third party should not fail a test job. It is wrong *unsupervised*: the check
# keeps its name, keeps reporting, and stops being able to say no. Telling the
# two apart by what a step "looks like" is an open set, so this uses the
# convention the repository already runs on (`# schedule-signal-exempt:` in
# ops-checks.yml, `# alert-delivery-exempt:` in no_new_wheels_lint): the
# exemption is written down next to the thing it exempts, and reviewed.
GATE_EXEMPT_RE = re.compile(r"#\s*gate-exempt:\s*(\S.*)")


def defanged_steps(workflow: dict, job_id: str, source: str = "") -> list[str]:
    """Steps in ``job_id`` whose failure cannot fail the job, minus the declared ones.

    The inventory already knows this matters at job level -- `infra_ci.
    vault_policy` is declared `blocks_merge: false` for exactly that reason --
    but the same keyword one level down was invisible to every auditor, so a
    required job could carry a permanently-green `ruff` step and all three
    passed it.

    ``source`` is the workflow's raw text, because YAML parsing discards the
    comments the exemptions live in. Without it every `continue-on-error` is
    reported, which is the safe direction: a caller that cannot supply the text
    gets the unfiltered list rather than a quietly shorter one.

    Job level is reported as ``"<job>"`` so one caller handles both.
    """
    job = (workflow.get("jobs") or {}).get(job_id) or {}
    exempt = _exempt_lines(source)
    out: list[str] = []
    if job.get("continue-on-error") and not exempt:
        out.append("<job>")
    for index, step in enumerate(job.get("steps") or []):
        if not step.get("continue-on-error"):
            continue
        name = str(step.get("name") or step.get("uses") or f"step #{index}")
        if name not in exempt:
            out.append(name)
    return out


def _exempt_lines(source: str) -> set[str]:
    """Step names carrying a `# gate-exempt:` comment.

    Matched by the step's own `- name:` because that is what a reader sees in
    the report; a comment attached to an unnamed step exempts nothing, which is
    deliberate -- an exemption nobody can point at is not one.
    """
    if not source:
        return set()
    found: set[str] = set()
    current = ""
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("- name:"):
            current = stripped[len("- name:") :].strip().strip("\"'")
        elif GATE_EXEMPT_RE.search(stripped) and current:
            found.add(current)
    return found
