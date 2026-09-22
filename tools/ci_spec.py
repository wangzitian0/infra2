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
#
# All three now read through here (#788 review, which caught this comment
# claiming a consolidation that had only been done for one of them). What the
# inventory YAML does is a different question and stays with its own readers.
# ---------------------------------------------------------------------------

import re  # noqa: E402
import yaml  # noqa: E402  (kept next to the helpers that use it)


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
        # The text read; only the structure did not. Handing back the source
        # keeps the comment scan working on a file whose YAML is broken.
        return {}, source
    # Valid YAML that is not a mapping (`null`, a list, a bare string) would sail
    # past the handler and break every `.get()` downstream.
    return (parsed if isinstance(parsed, dict) else {}), source


def workflow_read_error(path) -> str | None:
    """Why `read_workflow` gave back an empty mapping for `path`, or None.

    Only the linter needs this: an auditor that finds nothing to audit reports
    that, while a linter owes the reader the reason a file did not parse. Called
    on the empty-mapping path only, so the happy path still parses once.

    An absent or blank file is not an error here -- `lint_workflow` has always
    treated those as "no findings", and turning them into failures would be a
    behaviour change smuggled in under a refactor.
    """
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
        parsed = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        # "Unreadable" is not only malformed YAML (#788 review): a permission
        # error or invalid UTF-8 raised straight through the docstring's promise
        # and crashed the caller. A workflow that cannot be read is a finding to
        # phrase, in every one of those cases.
        return {}
    # Valid YAML that is not a mapping (`null`, a list, a bare string) would sail
    # past the try and break every `.get()` downstream.
    return parsed if isinstance(parsed, dict) else {}


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
    jobs = workflow.get("jobs")
    job = (jobs if isinstance(jobs, dict) else {}).get(job_id)
    if not isinstance(job, dict):
        # A scalar or list where a job mapping belongs (#788 review). `.get()` on
        # it raises and takes the auditor down; there is nothing to audit, and a
        # crash is not a finding.
        return []
    exempt = _exempt_lines(source)
    out: list[str] = []
    # Keyed by THIS job's id, not `not exempt` (#788 review). Treating the
    # exemptions as one workflow-wide flag meant a single exempted step -- a
    # coverage upload, say -- silenced a job-level `continue-on-error` somewhere
    # else in the same file. That is the bypass this audit exists to catch,
    # rebuilt inside the audit.
    if job.get("continue-on-error") and job_id not in exempt:
        out.append("<job>")
    steps = job.get("steps")
    for index, step in enumerate(steps if isinstance(steps, list) else []):
        if not isinstance(step, dict) or not step.get("continue-on-error"):
            continue
        name = str(step.get("name") or step.get("uses") or f"step #{index}")
        if name not in exempt:
            out.append(name)
    return out


# A job declaration: a two-space-indented key inside the top-level `jobs:` block.
_JOB_KEY_RE = re.compile(r"^  ([A-Za-z_][\w-]*):\s*$")


def _exempt_lines(source: str) -> set[str]:
    """Names carrying a `# gate-exempt:` comment, keyed to the declaration above it.

    A declaration is a job id or a step's `- name:` -- what a reader sees in the
    report. A comment attached to neither exempts nothing, which is deliberate: an
    exemption nobody can point at is not one. Keying to exactly one declaration is
    the whole point; see `defanged_steps` for what an unkeyed exemption did.
    """
    if not source:
        return set()
    found: set[str] = set()
    current = ""
    in_jobs = False
    for line in source.splitlines():
        # Split off comments before reading structure, so `- name: x  # gate-exempt: y`
        # yields the name `x`. A `#` inside a quoted step name would be mis-split;
        # no workflow here has one, and the failure direction is to report the step
        # rather than to hide it.
        code = line.split("#", 1)[0]
        if code[:1] not in ("", " ", "\t"):
            in_jobs = code.startswith("jobs:")
        elif in_jobs:
            job = _JOB_KEY_RE.match(code.rstrip())
            if job:
                current = job.group(1)
        stripped = code.strip()
        # `== "-"` as well as the prefix: the comment is split off first, so a
        # line that is nothing but `- # gate-exempt: ...` strips down to a bare
        # dash -- which is exactly the unnamed step this reset exists for.
        if stripped == "-" or stripped.startswith("- "):
            # A new list item, so whatever was declared above it is over. Without
            # this reset (#788 review) a bare `- # gate-exempt: ...` on an unnamed
            # step kept `current` pointing at the enclosing job and exempted the
            # job's own `continue-on-error` -- the same "one comment silences
            # something else" bypass this module was just fixed for, one level down.
            # `name` then `uses` because that is the order `defanged_steps` names a
            # step by; anything it reports as `step #N` is unexemptable on purpose.
            current = ""
            for key in ("- name:", "- uses:"):
                if stripped.startswith(key):
                    current = stripped[len(key) :].strip().strip("\"'")
        if current and GATE_EXEMPT_RE.search(line):
            found.add(current)
    return found
