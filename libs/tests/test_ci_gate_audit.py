"""Drift audit for the infra CI gate inventory (#461).

Also covers the scope-reporting contract added when the audit's coverage boundary turned
out to be undocumented anywhere a reader could actually see it (only a source comment said
"Phase 1: only infra-ci", while KNOWN_CI_WORKFLOWS never grew past that and the epic that
would have grown it, Infra-016 #459, was closed "not planned"): `audit()`'s JSON output must
itself name which workflows are covered and which are not, and infra-ci.yml's own step must
run with `--enforce` now that it is provably fully covered.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from tools.ci_gate_audit import (
    PR_TRIGGERS,
    WORKFLOWS_DIR,
    _all_workflow_files,
    audit,
    audit_gates,
    covered_workflows,
    main,
)

ROOT = Path(__file__).resolve().parents[2]
INFRA_CI = ".github/workflows/infra-ci.yml"


def _gate(**over):
    base = dict(
        id="infra_ci.compose_validate",
        stage="github_ci.merge_authority",
        task_category="compose_validate",
        workflow=INFRA_CI,
        job="validate-compose",
    )
    base.update(over)
    return base


def test_real_inventory_has_clean_schema_and_no_dangling() -> None:
    result = audit()
    assert result["schema_errors"] == []
    assert result["dangling_gates"] == []


def test_infra_ci_is_fully_covered() -> None:
    # every job of infra-ci.yml is registered (no unregistered within the covered workflow)
    result = audit()
    assert [u for u in result["unregistered_jobs"] if "infra-ci.yml" in u] == []


def test_report_mode_exit_zero_when_clean() -> None:
    assert main([]) == 0


def test_dangling_gate_is_hard() -> None:
    r = audit_gates(
        [_gate(job="does-not-exist")],
        root=ROOT,
        prefix="infra_ci.",
        known_ci_workflows=(),
    )
    assert r["dangling_gates"]


def test_unknown_stage_is_schema_error() -> None:
    r = audit_gates(
        [_gate(stage="bogus.stage")],
        root=ROOT,
        prefix="infra_ci.",
        known_ci_workflows=(),
    )
    assert any("unknown stage" in e for e in r["schema_errors"])


def test_wrong_prefix_is_schema_error() -> None:
    r = audit_gates(
        [_gate(id="ci.compose_validate")],
        root=ROOT,
        prefix="infra_ci.",
        known_ci_workflows=(),
    )
    assert any("prefix" in e for e in r["schema_errors"])


def test_unregistered_job_detected() -> None:
    # cover only one job of infra-ci.yml -> the other jobs are unregistered
    r = audit_gates(
        [_gate()], root=ROOT, prefix="infra_ci.", known_ci_workflows=(INFRA_CI,)
    )
    assert any("infra-ci.yml" in u for u in r["unregistered_jobs"])


def test_empty_workflow_does_not_crash() -> None:
    # a gate with empty `workflow` resolves to the repo root dir; the audit must report it
    # (dangling + schema error), not crash with IsADirectoryError.
    r = audit_gates(
        [_gate(workflow="")], root=ROOT, prefix="infra_ci.", known_ci_workflows=()
    )
    assert r["dangling_gates"] and r["schema_errors"]


# -- Scope-reporting contract -------------------------------------------------------


def test_audit_output_names_its_own_coverage_boundary() -> None:
    """A reader of the printed JSON (not just this source file) must be able to
    see which workflows the audit does and does not check."""
    result = audit()
    assert result["covered_workflows"] == sorted(covered_workflows(ROOT))
    assert result["out_of_scope_workflows"], (
        "sanity: infra2 has more than one workflow, so some must be out of scope"
    )
    assert set(result["covered_workflows"]).isdisjoint(result["out_of_scope_workflows"])

    # Every workflow file is accounted for on one side or the other -- a new workflow
    # must show up as newly out-of-scope, not vanish silently. Delegates to the tool's
    # OWN enumeration (`_all_workflow_files`) rather than re-globbing here: a hand-rolled
    # `*.yml`-only copy of that scan (as this line used to be, PR #780 review) silently
    # stops catching new `.yaml` workflows the moment it and the tool's real `*.yml` +
    # `*.yaml` scan drift apart -- see test_all_workflow_files_includes_dot_yaml below
    # for that failure mode reproduced directly.
    all_workflows = set(_all_workflow_files(ROOT))
    accounted = set(result["covered_workflows"]) | set(result["out_of_scope_workflows"])
    assert all_workflows <= accounted


def test_all_workflow_files_includes_dot_yaml(tmp_path) -> None:
    """Regression for PR #780 review: a `.yaml`-suffixed workflow must not be able
    to vanish from the scope-reporting scan the way a `*.yml`-only enumeration
    would let it -- exactly the gap this PR's audit fix exists to close, caught by
    review in this test file's own coverage-boundary check.

    Constructs the scenario directly (the live repo currently has zero `.yaml`
    workflows, so this can't be exercised against ROOT) and contrasts the two
    enumeration strategies: a hand-rolled `*.yml`-only glob (what the flagged line
    used to do) against `_all_workflow_files` (what it delegates to after the fix).
    """
    # Built via WORKFLOWS_DIR/variable interpolation rather than a spelled-out
    # ".github/workflows/*.yaml" literal: libs/tests/test_workflow_reference_contract.py
    # scans this repo's own source for exactly that literal shape and requires it to
    # resolve to a real workflow file -- this fixture name deliberately never exists.
    new_yaml_name = "new-check.yaml"
    extra_yaml_workflow = f"{WORKFLOWS_DIR}/{new_yaml_name}"

    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "infra-ci.yml").write_text("on: push\njobs: {}\n", encoding="utf-8")
    (wf_dir / new_yaml_name).write_text("on: push\njobs: {}\n", encoding="utf-8")

    # OLD assertion style: re-globs *.yml locally instead of sharing the tool's scan.
    old_style_scan = {f"{WORKFLOWS_DIR}/{p.name}" for p in wf_dir.glob("*.yml")}
    assert extra_yaml_workflow not in old_style_scan, (
        "reproduces the bug under review: the old *.yml-only scan lets a .yaml "
        "workflow vanish silently"
    )

    # NEW assertion style: delegates to the tool's own enumeration.
    fixed_scan = set(_all_workflow_files(tmp_path))
    assert extra_yaml_workflow in fixed_scan, (
        "the fixed scan (sharing tools.ci_gate_audit._all_workflow_files) must "
        "catch the .yaml workflow that the old scan missed"
    )
    assert f"{WORKFLOWS_DIR}/infra-ci.yml" in fixed_scan


def test_enforce_mode_fails_closed_on_an_unregistered_job(monkeypatch, capsys) -> None:
    """Within the covered scope, --enforce must actually turn drift into a
    non-zero exit -- not just a printed line. AND (#780 review) the error text
    itself must not overclaim scope: it must not tell a reader every job in
    every workflow needs coordinate-izing when the audit only ever checks
    covered_workflows -- that would hide the exact same gap this PR exists to
    stop hiding, just moved from a source comment into the error message."""
    import tools.ci_gate_audit as mod

    fake_result = {
        "schema_errors": [],
        "dangling_gates": [],
        "unregistered_jobs": [f"{INFRA_CI}:some-new-job"],
        "covered_workflows": [INFRA_CI],
        "out_of_scope_workflows": ["some/other.yml"],
    }
    monkeypatch.setattr(mod, "audit", lambda root=mod.ROOT: fake_result)

    assert mod.main(["--enforce"]) == 1
    stderr = capsys.readouterr().err
    assert "every job" not in stderr.lower(), (
        f"error text overclaims scope with an unqualified 'every job' -- a reader "
        f"would think out_of_scope_workflows need registering too: {stderr!r}"
    )
    assert INFRA_CI in stderr, (
        f"error text must name the actual covered_workflows scope, not just gesture "
        f"at 'every job': {stderr!r}"
    )
    assert "not backlog" in stderr.lower(), (
        f"error text must say out-of-scope workflows are not an unregistered "
        f"backlog: {stderr!r}"
    )
    assert "pull request" in stderr.lower(), (
        f"error text must state the coverage CRITERION (runs on a pull request) so a "
        f"reader can check their own workflow against it, rather than making them go "
        f"find a list: {stderr!r}"
    )

    capsys.readouterr()  # drain before the report-only check below
    assert mod.main([]) == 0, (
        "report-only mode must stay non-blocking on the same drift"
    )


def test_infra_ci_step_runs_the_audit_with_enforce() -> None:
    """infra-ci.yml's own ci_gate_audit step must pass --enforce now that
    infra-ci.yml is provably fully covered (see the test above this one)."""
    workflow = yaml.safe_load((ROOT / INFRA_CI).read_text(encoding="utf-8"))
    steps = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "tools.ci_gate_audit" in str(step.get("run") or "")
    ]
    assert len(steps) == 1, f"expected exactly one ci_gate_audit step, got {steps}"
    assert "--enforce" in steps[0]["run"]


# -- Coverage criterion: computed from triggers, not hand-listed ---------------------


def _repo(tmp_path, workflows: dict[str, str]) -> object:
    wf_dir = tmp_path / WORKFLOWS_DIR
    wf_dir.mkdir(parents=True)
    for name, body in workflows.items():
        (wf_dir / name).write_text(body, encoding="utf-8")
    return tmp_path


def test_coverage_is_computed_from_the_pull_request_trigger(tmp_path) -> None:
    """The scope must follow the workflows' own triggers.

    A hand-written scope tuple cannot tell "deliberately excluded" apart from "added
    last Tuesday and nobody noticed": the risk the inventory guards is a job showing up
    on a PR with nobody having decided whether it gates, and that risk is created by
    adding `pull_request:` -- not by anyone remembering to widen a constant.
    """
    root = _repo(
        tmp_path,
        {
            # bare `on:` -> PyYAML gives the boolean True as the key, the shape every
            # real workflow in this repo has.
            "on-pr.yml": "on:\n  pull_request:\n    paths: ['x/**']\njobs:\n  a: {}\n",
            "on-pr-target.yml": "on:\n  pull_request_target:\njobs:\n  b: {}\n",
            "scheduled-only.yml": "on:\n  schedule:\n    - cron: '0 1 * * *'\njobs:\n  c: {}\n",
            "push-only.yml": "on: push\njobs:\n  d: {}\n",
        },
    )
    covered = set(covered_workflows(root))
    assert covered == {
        f"{WORKFLOWS_DIR}/on-pr.yml",
        f"{WORKFLOWS_DIR}/on-pr-target.yml",
    }, covered


def test_adding_a_pull_request_trigger_widens_the_scope_by_itself(tmp_path) -> None:
    """The regression a frozen tuple allowed: a workflow gains `pull_request:` (so its
    jobs start appearing as PR checks) and the audit keeps not looking at it."""
    wf = tmp_path / WORKFLOWS_DIR / "later.yml"
    root = _repo(
        tmp_path,
        {"later.yml": "on:\n  schedule:\n    - cron: '0 1 * * *'\njobs:\n  j: {}\n"},
    )
    assert covered_workflows(root) == ()

    wf.write_text(
        "on:\n  schedule:\n    - cron: '0 1 * * *'\n  pull_request:\njobs:\n  j: {}\n",
        encoding="utf-8",
    )
    assert covered_workflows(root) == (f"{WORKFLOWS_DIR}/later.yml",)


def test_every_pr_triggered_workflow_is_fully_registered() -> None:
    """The live ratchet: nothing that can put a check on a PR is unregistered."""
    result = audit()
    assert result["unregistered_jobs"] == []
    # And the scope is genuinely wider than the single workflow it used to be frozen at,
    # so this test cannot pass vacuously.
    assert len(result["covered_workflows"]) >= 3, result["covered_workflows"]


def test_out_of_scope_workflows_really_cannot_gate_a_pull_request() -> None:
    """Whatever the audit excuses itself from checking must be excused for the stated
    reason -- not merely absent from a list someone forgot to update."""
    from tools.ci_gate_audit import _workflow_triggers

    for wf in audit()["out_of_scope_workflows"]:
        assert not (_workflow_triggers(ROOT / wf) & PR_TRIGGERS), (
            f"{wf} is reported out of scope but does run on a pull request"
        )


def test_widening_coverage_did_not_change_what_blocks_merge() -> None:
    """Direction proof for the ops-checks/docs backfill.

    Registering a job is bookkeeping; `blocks_merge: true` is authority. The backfill
    added 13 gates and zero authority, which is what makes it provably non-loosening --
    and this set is exactly the 7 required status checks configured on the live `main`
    ruleset. Promoting a gate here without adding its ruleset context (or vice versa)
    breaks that correspondence, so this list is deliberately spelled out rather than
    derived from the same file it is checking.
    """
    inventory = yaml.safe_load(
        (ROOT / "docs/ssot/ci-gate-inventory.yaml").read_text(encoding="utf-8")
    )
    blocking = {g["id"] for g in inventory["gates"] if g.get("blocks_merge")}
    assert blocking == {
        "infra_ci.compose_validate",
        "infra_ci.deployer_logic",
        "infra_ci.op_healthcheck",
        "infra_ci.vault_agent",
        "infra_ci.deployer_classes",
        "infra_ci.lint_python",
        "infra_ci.harness_check",
    }, (
        "the blocks_merge set changed; if that is intended, update the live branch "
        "ruleset's required_status_checks in the same change and edit this list"
    )


def test_the_enforce_step_can_actually_fire_on_every_covered_workflow() -> None:
    """A guard that never runs is not a guard.

    `ci_gate_audit --enforce` lives in infra-ci.yml, which is itself `paths`-filtered.
    Before this test, those paths named five workflows by hand and omitted docs.yml --
    so adding an unregistered job to docs.yml changed no path infra-ci watches, infra-ci
    never ran, and the coverage check silently did not happen on exactly the change it
    exists to catch. Every workflow the audit claims to cover must be able to trigger
    the workflow that runs the audit.
    """
    infra_ci = yaml.safe_load((ROOT / INFRA_CI).read_text(encoding="utf-8"))
    triggers = infra_ci.get(True, infra_ci.get("on")) or {}

    def matches(patterns: list[str], target: str) -> bool:
        # GitHub `paths` globs: `**` spans directory separators, `*` does not. Only the
        # shapes this repo actually uses are handled -- a new shape that this cannot
        # read will read as "no match" and fail the test loudly rather than pass blind.
        for pat in patterns:
            if pat == target:
                return True
            if pat.endswith("/**") and target.startswith(pat[:-2]):
                return True
        return False

    for event in ("pull_request", "push"):
        paths = (triggers.get(event) or {}).get("paths") or []
        assert paths, f"infra-ci.yml has no {event} paths filter to check"
        for wf in covered_workflows(ROOT):
            assert matches(paths, wf), (
                f"{wf} is in the audit's covered scope, but editing it does not trigger "
                f"infra-ci.yml on {event} -- the --enforce step would never run"
            )
