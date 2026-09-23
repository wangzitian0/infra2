"""Single source of truth for cross-repo CI testing hierarchy and compute budgets (#788, #840).

Canonical domain location in libs.core.
Defines the Left-to-Right Testing Hierarchy (L0 Gate -> L1 Integrate -> L2 Heavy -> L3 Release):
- L0 PR Gate: Total wall clock <= 90s, individual test <= 30s, shards between 2 and 8.
- L2 Heavy / Nightly: Benchmarks, full reconciliation, and soak testing.
"""

from __future__ import annotations

import re
import yaml

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


def read_workflow(path) -> tuple[dict, str]:
    """A workflow's parsed YAML and its raw text, from a single read.

    Two reads meant two failure paths and only one of them handled (#788 review):
    `load_workflow` returned {} for an unreadable file while the caller's own
    `read_text()` -- needed because YAML parsing discards the comments exemptions
    live in -- raised straight through it. One read, one failure mode.
    """
    from pathlib import Path

    p = Path(path)
    if not p.is_file():
        return {}, ""
    try:
        source = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}, ""
    try:
        parsed = yaml.safe_load(source)
    except yaml.YAMLError:
        return {}, source
    return (parsed if isinstance(parsed, dict) else {}), source


def workflow_read_error(path) -> str | None:
    """Why `read_workflow` gave back an empty mapping for `path`, or None."""
    from pathlib import Path

    p = Path(path)
    if not p.is_file():
        return None
    try:
        source = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return str(exc)
    if not source.strip():
        return None
    try:
        parsed = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        return str(exc)
    if isinstance(parsed, dict):
        return None
    return f"top level is {type(parsed).__name__}, not a mapping"


def load_workflow(path) -> dict:
    """A workflow's parsed YAML, or {} when it is absent or unreadable."""
    from pathlib import Path

    p = Path(path)
    if not p.is_file():
        return {}
    try:
        parsed = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


GATE_EXEMPT_RE = re.compile(r"#\s*gate-exempt:\s*(\S.*)")


def defanged_steps(workflow: dict, job_id: str, source: str = "") -> list[str]:
    """Steps in ``job_id`` whose failure cannot fail the job, minus the declared ones."""
    jobs = workflow.get("jobs")
    job = (jobs if isinstance(jobs, dict) else {}).get(job_id)
    if not isinstance(job, dict):
        return []
    exempt = _exempt_lines(source)
    out: list[str] = []
    if job.get("continue-on-error") and (job_id, job_id) not in exempt:
        out.append("<job>")
    steps = job.get("steps")
    for index, step in enumerate(steps if isinstance(steps, list) else []):
        if not isinstance(step, dict) or not step.get("continue-on-error"):
            continue
        name = str(step.get("name") or step.get("uses") or f"step #{index}")
        if (job_id, name) not in exempt:
            out.append(name)
    return out


_JOB_KEY_RE = re.compile(r"^  ([A-Za-z_][\w-]*):\s*$")
_BLOCK_SCALAR_RE = re.compile(r":\s*[|>][+-]?\d*\s*$")


def _exempt_lines(source: str) -> set[tuple[str, str]]:
    """`(job id, 声明名)` -> 带 `# gate-exempt:` 的那些，按 **job 隔离**."""
    if not source:
        return set()
    found: set[tuple[str, str]] = set()
    job_id = ""
    current = ""
    in_jobs = False
    block_indent: int | None = None
    for line in source.splitlines():
        indent = len(line) - len(line.lstrip())
        if block_indent is not None:
            if not line.strip() or indent > block_indent:
                continue
            block_indent = None
        code = line.split("#", 1)[0]
        if code[:1] not in ("", " ", "\t"):
            in_jobs = code.startswith("jobs:")
            if not in_jobs:
                job_id = current = ""
        elif in_jobs:
            job = _JOB_KEY_RE.match(code.rstrip())
            if job:
                job_id = job.group(1)
                current = job_id
        stripped = code.strip()
        if stripped == "-" or stripped.startswith("- "):
            current = ""
            for key in ("- name:", "- uses:"):
                if stripped.startswith(key):
                    current = stripped[len(key) :].strip().strip("\"'")
        if _BLOCK_SCALAR_RE.search(code):
            block_indent = indent
        if job_id and current and GATE_EXEMPT_RE.search(line):
            found.add((job_id, current))
    return found


__all__ = [
    "BANNED_GATE_MARKERS",
    "BANNED_IN_GATE_PATTERNS",
    "GATE_EXEMPT_RE",
    "GATE_WALL_CLOCK_BUDGET_S",
    "MAX_SINGLE_TEST_S",
    "SHARD_MAX",
    "SHARD_MIN",
    "SOFT_WARN_S",
    "SPEC_VERSION",
    "TARGET_PER_SHARD_S",
    "defanged_steps",
    "load_workflow",
    "read_workflow",
    "workflow_read_error",
]
