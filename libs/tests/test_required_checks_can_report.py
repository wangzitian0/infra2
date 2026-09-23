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


def test_the_inventory_still_covers_every_ruleset_required_check() -> None:
    """The guard is only as wide as the inventory; a check missing from it is unguarded.

    Not a network call: the ruleset's seven contexts are the job *names* in the
    blocking workflows, so the two can be compared offline. A required check whose
    job is not registered as blocks_merge would slip past the parametrised test above.
    """
    data = yaml.safe_load(INVENTORY.read_text(encoding="utf-8"))
    blocking = {(g["workflow"], g["job"]) for g in data["gates"] if g.get("blocks_merge") is True}
    for rel in _blocking_workflows():
        jobs = yaml.safe_load((REPO / rel).read_text(encoding="utf-8"))["jobs"]
        for job_id, spec in jobs.items():
            if (rel, job_id) in blocking:
                assert spec.get("name"), f"{rel}:{job_id} blocks merge but has no display name"
