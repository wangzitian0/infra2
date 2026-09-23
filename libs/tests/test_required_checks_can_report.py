"""A required status check must be able to report on *any* pull request.

GitHub's branch protection waits for a named context unconditionally. A workflow
`paths:` filter is not a way to say "this check does not apply here" -- it is a
way to say "this workflow does not start", and a workflow that never starts never
reports anything. The pull request then sits at BLOCKED forever, waiting on a
status that will never arrive, and the only way out is a manual `gh pr merge`
that bypasses the gate entirely. A guard that gets bypassed stops being a guard.

This has now happened twice, each time when a new top-level directory appeared:

- #817: a PR touching only `harness/workspace/*.md` plus AGENTS.md. Fixed by
  hand-adding `harness/**` to the list.
- #850: a PR touching only `skills/**`, a directory that did not exist when the
  list was last edited. The hand-added entry above did not help, because the
  problem was never `harness/` specifically.

Adding one more glob fixes the instance and guarantees the recurrence, so the
assertion here is about the *shape*: a workflow that hosts a merge-blocking gate
must not gate its own pull_request trigger on paths at all. Cost control belongs
inside the workflow, where infra-ci.yml already puts it -- `detect-changes` runs
unconditionally and every gated job is skipped via `if:`, which still reports a
conclusion that branch protection accepts.

The scope is computed from the gate inventory rather than naming infra-ci.yml, so
promoting any future check to blocks_merge: true extends this guard with it.

Two older guards asked the narrower question "is *this* entry present in the
list": test_layer_coverage.py checked every registered service layer, and
test_ci_gate_audit.py checked every workflow the coverage audit claims to cover.
Both become vacuous once there is no list, and a vacuous guard is the quiet kind
of failure -- so both were rewritten to assert this same structural fact rather
than left to pass over an empty check.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
INVENTORY = REPO / "docs" / "ssot" / "ci-gate-inventory.yaml"


def _blocking_workflows() -> list[str]:
    data = yaml.safe_load(INVENTORY.read_text(encoding="utf-8"))
    paths = sorted(
        {g["workflow"] for g in data["gates"] if g.get("blocks_merge") is True}
    )
    assert paths, (
        "no blocks_merge gate found in the inventory -- either the schema changed "
        "or this guard is now scoped to nothing, which is the same as deleted"
    )
    return paths


def _on_block(workflow: Path) -> dict:
    """`on:` parses as the boolean True in YAML 1.1, which is why this is not `doc['on']`."""
    doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    for key in ("on", True):
        if key in doc:
            return doc[key]
    raise AssertionError(f"{workflow} has no trigger block")


@pytest.mark.parametrize("event", ("pull_request", "push"))
@pytest.mark.parametrize("rel", _blocking_workflows())
def test_a_merge_blocking_workflow_has_no_path_filter(rel: str, event: str) -> None:
    workflow = REPO / rel
    assert workflow.is_file(), f"inventory points at a missing workflow: {rel}"
    on = _on_block(workflow)
    # Membership, not truthiness: a bare `pull_request:` with nothing under it
    # parses as None, and that is the *most* permissive form -- the one this
    # guard exists to require. Testing the value would reject the fix itself.
    assert event in on, (
        f"{rel} hosts a merge-blocking gate but does not run on {event}, "
        "so its required checks can never report"
    )
    pr = on[event] or {}
    filtered = sorted(k for k in ("paths", "paths-ignore") if k in pr)
    assert not filtered, (
        f"{rel} hosts a merge-blocking gate and filters its {event} trigger on "
        f"{filtered}. Any PR whose diff falls outside that filter never starts this "
        "workflow, so its required checks never report and the PR is permanently "
        "BLOCKED (#817, #850). Skip the work inside the workflow with `if:` instead -- "
        "a skipped job still reports a conclusion; an absent workflow does not."
    )


def test_every_blocking_gate_resolves_to_a_real_named_job() -> None:
    """A gate whose job cannot be resolved is a check that can never be satisfied.

    The ruleset names its required checks by *display name*. `pr_merge_gate`
    reconstructs those names by reading each blocks_merge gate's `job` key out of
    its workflow and taking that job's `name:` -- and when the lookup fails it
    silently falls back to the job key itself. A gate pointing at a renamed or
    deleted job therefore produces a name matching no ruleset context, so the gate
    is reported as "never reported" on every PR, forever, with nothing saying why.

    That is checkable offline, which the ruleset itself is not: comparing against
    the live required-check set means a network call, and writing the seven
    contexts down here would be a second copy of them that drifts. So this asserts
    the resolution step instead -- the one that can fail silently.

    (An earlier version of this test was named for the ruleset comparison and only
    asserted that jobs had a `name`. Copilot caught the mismatch on #853. A test
    that promises more than its body does is worse than no test, because the
    promise is what gets read.)
    """
    data = yaml.safe_load(INVENTORY.read_text(encoding="utf-8"))
    blocking = [g for g in data["gates"] if g.get("blocks_merge") is True]
    assert blocking, "no blocking gate to check"

    display_names: list[str] = []
    for gate in blocking:
        workflow = REPO / gate["workflow"]
        jobs = yaml.safe_load(workflow.read_text(encoding="utf-8")).get("jobs") or {}
        job = gate["job"]
        assert job in jobs, (
            f"gate {gate['id']!r} points at job {job!r}, which does not exist in "
            f"{gate['workflow']}. pr_merge_gate falls back to the job key as the "
            "display name, which matches no ruleset context, so this gate would "
            "read as 'never reported' on every pull request"
        )
        name = (jobs[job] or {}).get("name")
        assert name, (
            f"gate {gate['id']!r} resolves to job {job!r}, which has no `name:`. "
            "The ruleset matches on display name; without one the fallback is the "
            "job key, which is not what GitHub reports"
        )
        display_names.append(name)

    duplicates = {n for n in display_names if display_names.count(n) > 1}
    assert not duplicates, (
        f"two blocking gates share the display name(s) {sorted(duplicates)}; "
        "pr_merge_gate holds them in a set, so one silently vanishes from the "
        "list of checks it insists on seeing"
    )
