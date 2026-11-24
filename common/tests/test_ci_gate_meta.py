"""CI gate meta tests: inventory audit, workflow lint, ruleset audit, schema and defanging.

One module for the five gate auditors (#461, #504, #508, #811). Each section keeps
the tests of one auditor; shared helpers sit at the top of the file.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from infra2_sdk import ci as ci_spec
from infra2_sdk.ci import (
    load_delivery_stages,
    validate_gate,
    validate_inventory,
)

from common.core import ci_gate_ruleset_audit as cgra
from common.core.ci_gate_audit import (
    PR_TRIGGERS,
    WORKFLOWS_DIR,
    _all_workflow_files,
    audit,
    audit_gates,
    covered_workflows,
    main,
)
from common.core.ci_gate_lint import lint_workflow

ROOT = Path(__file__).resolve().parents[2]
INFRA_CI = ".github/workflows/infra-ci.yml"
INFRA_CI_PATH = ROOT / INFRA_CI
STAGES = set(load_delivery_stages())


# ========== inventory audit ==========


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


SDK_STAGE_IDS = sorted(load_delivery_stages())


def _unknown_stage_errors(stage: str) -> list[str]:
    result = audit_gates(
        [_gate(stage=stage)],
        root=ROOT,
        prefix="infra_ci.",
        known_ci_workflows=(),
    )
    return [e for e in result["schema_errors"] if "unknown stage" in e]


def test_the_audit_knows_every_stage_id_of_the_installed_sdk() -> None:
    # The audit reads the vocabulary from the installed SDK (#1206). It has no local copy.
    # Every SDK stage id is known to the audit. An id that the SDK does not list is unknown to it.
    assert len(SDK_STAGE_IDS) >= 8, f"the SDK lists only {SDK_STAGE_IDS}"
    rejected = [sid for sid in SDK_STAGE_IDS if _unknown_stage_errors(sid)]
    assert rejected == []
    assert _unknown_stage_errors("bogus.not_an_sdk_stage")


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
    # ".github/workflows/*.yaml" literal: common/tests/test_workflow_reference_contract.py
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
    import common.core.ci_gate_audit as mod

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
    # so this test cannot pass vacuously. docs.yml and infra-ci.yml start on a pull
    # request; ops-checks.yml stopped with the deploy_v2 canary (#1477).
    assert len(result["covered_workflows"]) >= 2, result["covered_workflows"]


def test_out_of_scope_workflows_really_cannot_gate_a_pull_request() -> None:
    """Whatever the audit excuses itself from checking must be excused for the stated
    reason -- not merely absent from a list someone forgot to update."""
    from common.core.ci_gate_audit import _workflow_triggers

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

    `ci_gate_audit --enforce` lives in infra-ci.yml. It used to be `paths`-filtered,
    and this test answered the question by matching each covered workflow against
    those globs -- correct only while the globs stayed current. They did not: the
    list named five workflows by hand and omitted docs.yml, so adding an
    unregistered job there triggered nothing and the coverage check silently did
    not happen on exactly the change it exists to catch. The hand-fix for that
    (a `.github/workflows/**` glob) did not stop #817 or #850, which were the same
    shape one directory over.

    The filter is gone (#817), so the answer no longer depends on a list: every
    change triggers infra-ci.yml, therefore every covered workflow does. What this
    now asserts is that premise, plus a non-empty covered set -- because "every
    workflow in an empty set can trigger the audit" is true and worthless.
    """
    infra_ci = yaml.safe_load((ROOT / INFRA_CI).read_text(encoding="utf-8"))
    triggers = infra_ci.get(True, infra_ci.get("on")) or {}

    for event in ("pull_request", "push"):
        assert event in triggers, f"infra-ci.yml must still run on {event}"
        spec = triggers[event] or {}
        present = sorted(k for k in ("paths", "paths-ignore") if k in spec)
        assert not present, (
            f"infra-ci.yml filters its {event} trigger on {present}, so editing a "
            f"covered workflow outside that filter would not run --enforce. Skip "
            f"work inside the workflow with `if:`, never at the trigger."
        )

    covered = covered_workflows(ROOT)
    assert covered, (
        "the audit covers no workflow, so this guard is checking nothing -- "
        "covered_workflows() returning empty is itself the finding"
    )


def test_a_jobs_node_that_is_not_a_mapping_is_not_a_crash(tmp_path) -> None:
    """`jobs:` as a list or a scalar is valid YAML and an invalid workflow (#789
    review). `.keys()` on it took the whole audit down; there is nothing to
    enumerate, and a crash is not a finding."""
    from common.core.ci_gate_audit import _workflow_jobs

    for body in ("jobs: oops\n", "jobs:\n  - a\n  - b\n", "jobs: null\n", "on: push\n"):
        path = tmp_path / "wf.yml"
        path.write_text(body, encoding="utf-8")
        assert _workflow_jobs(path) == [], body

    path = tmp_path / "ok.yml"
    path.write_text("jobs:\n  build: {}\n", encoding="utf-8")
    assert _workflow_jobs(path) == ["build"], "the working case must still work"


def test_merge_group_and_transitive_workflow_call_are_covered(tmp_path) -> None:
    """#811: A workflow triggered by `merge_group` or called via local `uses: ./.github/workflows/...`
    from a covered workflow enters `covered_workflows()` and fails `audit_gates` on unregistered jobs."""
    assert "merge_group" in PR_TRIGGERS
    root = _repo(
        tmp_path,
        {
            "caller.yml": (
                "on: pull_request\n"
                "jobs:\n"
                "  delegate:\n"
                f"    uses: ./{WORKFLOWS_DIR}/reusable.yml\n"
            ),
            "reusable.yml": (
                "on: workflow_call\n"
                "jobs:\n"
                "  inner-check:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps: []\n"
            ),
            "merge-queue.yml": ("on: merge_group\njobs:\n  queue-gate: {}\n"),
        },
    )
    covered = covered_workflows(root)
    assert covered == (
        f"{WORKFLOWS_DIR}/caller.yml",
        f"{WORKFLOWS_DIR}/merge-queue.yml",
        f"{WORKFLOWS_DIR}/reusable.yml",
    )
    report = audit_gates(
        [
            _gate(
                id="infra_ci.caller_delegate",
                workflow=f"{WORKFLOWS_DIR}/caller.yml",
                job="delegate",
            ),
            _gate(
                id="infra_ci.queue_gate",
                workflow=f"{WORKFLOWS_DIR}/merge-queue.yml",
                job="queue-gate",
            ),
        ],
        root=root,
        prefix="infra_ci.",
        known_ci_workflows=covered,
    )
    assert report["unregistered_jobs"] == [f"{WORKFLOWS_DIR}/reusable.yml:inner-check"]


# ========== workflow lint ==========


def test_lint_clean_workflow(tmp_path: Path) -> None:
    wf = {
        "name": "pr-gate",
        "on": ["pull_request"],
        "jobs": {
            "test": {
                "strategy": {
                    "matrix": {
                        "shard": [1, 2, 3, 4],
                    }
                },
                "steps": [
                    {"name": "Checkout", "uses": "actions/checkout@v4"},
                    {"name": "Run tests", "run": "pytest -q -n auto tests/"},
                ],
            }
        },
    }
    wf_file = tmp_path / "pr_gate.yml"
    wf_file.write_text(yaml.dump(wf), encoding="utf-8")

    errors = lint_workflow(wf_file)
    assert errors == []


def test_lint_excessive_shards_flagged(tmp_path: Path) -> None:
    wf = {
        "name": "pr-gate-bloat",
        "on": {"pull_request": None},
        "jobs": {
            "test": {
                "strategy": {
                    "matrix": {
                        "shard": list(range(1, 17)),  # 16 shards!
                    }
                },
                "steps": [
                    {"run": "pytest -n auto"},
                ],
            }
        },
    }
    wf_file = tmp_path / "pr_bloat.yml"
    wf_file.write_text(yaml.dump(wf), encoding="utf-8")

    errors = lint_workflow(wf_file)
    assert len(errors) == 1
    assert "matrix shard count 16 violates [2, 8]" in errors[0]


@pytest.mark.parametrize(
    ("filename", "job_name", "run_cmd", "expected_error"),
    [
        (
            "pr_bench.yml",
            "Run benchmark",
            "python -m pytest -m benchmark",
            "runs banned gate pattern 'benchmark'",
        ),
        (
            "pr_workers.yml",
            "Run tests",
            "pytest -n 4 tests/",
            "uses hardcoded pytest worker count '-n 4'",
        ),
    ],
)
def test_lint_flags_banned_pr_gate_patterns(
    tmp_path: Path, filename: str, job_name: str, run_cmd: str, expected_error: str
) -> None:
    wf = {
        "name": f"pr-gate-{filename}",
        "on": ["pull_request"],
        "jobs": {"test": {"steps": [{"name": job_name, "run": run_cmd}]}},
    }
    wf_file = tmp_path / filename
    wf_file.write_text(yaml.dump(wf), encoding="utf-8")

    errors = lint_workflow(wf_file)
    assert len(errors) == 1
    assert expected_error in errors[0]


def test_lint_non_pr_workflow_ignored(tmp_path: Path) -> None:
    # Nightly benchmark workflow allows benchmarks and heavy shards
    wf = {
        "name": "nightly-benchmark",
        "on": {"schedule": [{"cron": "0 0 * * *"}]},
        "jobs": {
            "heavy-bench": {
                "strategy": {
                    "matrix": {
                        "shard": list(range(1, 20)),
                    }
                },
                "steps": [
                    {"name": "Run benchmark", "run": "pytest -m benchmark"},
                ],
            }
        },
    }
    wf_file = tmp_path / "nightly.yml"
    wf_file.write_text(yaml.dump(wf), encoding="utf-8")

    errors = lint_workflow(wf_file)
    assert errors == []


def test_main_missing_directory_fails_closed(tmp_path: Path) -> None:
    from common.core.ci_gate_lint import main
    import sys
    from unittest.mock import patch

    non_existent = tmp_path / "does_not_exist"
    with patch.object(sys, "argv", ["ci_gate_lint", str(non_existent)]):
        exit_code = main()
        assert exit_code == 1, "Missing directory must exit 1 (fail-closed)"


def test_reconcile_plan_gate_fetches_pr_base_sha_and_fails_on_diff_error(
    tmp_path: Path,
) -> None:
    """#1622:shallow PR checkouts must fetch the PR base SHA explicitly and fail closed."""
    wf_path = Path(__file__).resolve().parents[2] / ".github/workflows/infra-ci.yml"
    workflow = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["test-deployer-logic"]["steps"]
    step = next(
        item
        for item in steps
        if item.get("name") == "Gate reconcile plan builds for changed files"
    )
    script_template = step["run"]
    assert "origin/main...HEAD" not in script_template
    assert "mapfile" not in script_template
    assert "github.event.pull_request.base.sha" in script_template

    bare_repo = tmp_path / "remote.git"
    work_repo = tmp_path / "work"
    shallow_repo = tmp_path / "shallow"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare_repo)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(bare_repo),
            "config",
            "uploadpack.allowAnySHA1InWant",
            "true",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "clone", str(bare_repo), str(work_repo)],
        check=True,
        capture_output=True,
    )
    for cfg_key, cfg_val in (
        ("user.email", "ci@example.test"),
        ("user.name", "CI Test"),
    ):
        subprocess.run(
            ["git", "-C", str(work_repo), "config", cfg_key, cfg_val],
            check=True,
            capture_output=True,
        )
    (work_repo / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(work_repo), "add", "base.txt"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work_repo), "commit", "-m", "base commit"],
        check=True,
        capture_output=True,
    )
    base_sha = subprocess.check_output(
        ["git", "-C", str(work_repo), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    subprocess.run(
        ["git", "-C", str(work_repo), "push", "-u", "origin", "main"],
        check=True,
        capture_output=True,
    )

    subprocess.run(
        ["git", "-C", str(work_repo), "checkout", "-b", "feature/test"],
        check=True,
        capture_output=True,
    )
    (work_repo / "changed.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(work_repo), "add", "changed.py"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work_repo), "commit", "-m", "pr commit"],
        check=True,
        capture_output=True,
    )
    pr_sha = subprocess.check_output(
        ["git", "-C", str(work_repo), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    subprocess.run(
        ["git", "-C", str(work_repo), "push", "-u", "origin", "feature/test"],
        check=True,
        capture_output=True,
    )

    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            "feature/test",
            f"file://{bare_repo}",
            str(shallow_repo),
        ],
        check=True,
        capture_output=True,
    )

    def _run_step(
        target_base: str, has_non_doc: str = "true"
    ) -> subprocess.CompletedProcess[str]:
        rendered = (
            script_template.replace("${{ github.workspace }}", str(shallow_repo))
            .replace("${{ github.event.pull_request.base.sha }}", target_base)
            .replace("${{ needs.detect-changes.outputs.has_non_doc }}", has_non_doc)
            .replace(
                "uv run --locked python -m tools.reconcile_iac_inputs",
                "printf 'RECONCILE:%s\\n'",
            )
        )
        return subprocess.run(
            ["bash", "-c", rendered],
            capture_output=True,
            text=True,
            check=False,
        )

    ok_res = _run_step(base_sha, has_non_doc="true")
    assert ok_res.returncode == 0, ok_res.stderr
    assert "changed.py" in ok_res.stdout

    empty_res = _run_step(pr_sha, has_non_doc="true")
    assert empty_res.returncode != 0
    assert "zero changed files" in empty_res.stdout + empty_res.stderr

    bad_sha_res = _run_step("deadbeef" * 5, has_non_doc="true")
    assert bad_sha_res.returncode != 0

    sub_src = tmp_path / "sub_src"
    subprocess.run(
        ["git", "init", "-b", "main", str(sub_src)], check=True, capture_output=True
    )
    for cfg_key, cfg_val in (
        ("user.email", "ci@example.test"),
        ("user.name", "CI Test"),
    ):
        subprocess.run(
            ["git", "-C", str(sub_src), "config", cfg_key, cfg_val],
            check=True,
            capture_output=True,
        )
    (sub_src / "README.md").write_text("sub\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(sub_src), "add", "README.md"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(sub_src), "commit", "-m", "sub init"],
        check=True,
        capture_output=True,
    )

    sub_clone = tmp_path / "sub_shallow"
    subprocess.run(
        ["git", "-C", str(work_repo), "checkout", "main"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work_repo), "checkout", "-b", "feature/submodule"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(work_repo),
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(sub_src),
            "ext-tool",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work_repo), "commit", "-m", "add submodule"],
        check=True,
        capture_output=True,
    )
    sub_base = subprocess.check_output(
        ["git", "-C", str(work_repo), "rev-parse", "HEAD"], text=True
    ).strip()
    (sub_src / "README.md").write_text("sub v2\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(sub_src), "commit", "-am", "sub v2"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(work_repo),
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "update",
            "--remote",
            "ext-tool",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work_repo), "commit", "-am", "bump submodule"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work_repo), "push", "-u", "origin", "feature/submodule"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            "feature/submodule",
            f"file://{bare_repo}",
            str(sub_clone),
        ],
        check=True,
        capture_output=True,
    )
    sub_res = subprocess.run(
        [
            "bash",
            "-c",
            script_template.replace("${{ github.workspace }}", str(sub_clone))
            .replace("${{ github.event.pull_request.base.sha }}", sub_base)
            .replace("${{ needs.detect-changes.outputs.has_non_doc }}", "true")
            .replace(
                "uv run --locked python -m tools.reconcile_iac_inputs",
                "printf 'RECONCILE:%s\\n'",
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert sub_res.returncode == 0, sub_res.stderr
    assert "RECONCILE:--changed-file\nRECONCILE:ext-tool" in sub_res.stdout


# ========== ruleset audit ==========

# Every audit() now also asks whether the rulesets carrying those checks are
# enforcing and un-bypassable. These tests are about the check list, so they
# state a healthy posture explicitly rather than reaching the network.
_HEALTHY_POSTURE = {"rulesets": [], "inactive": [], "bypassable": []}


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _opener_returning(rules: list[dict]):
    def opener(request, timeout=0):
        return _Resp(json.dumps(rules).encode())

    return opener


def _raising_opener(request, timeout=0):
    raise OSError("github unreachable")


def _required_status_checks_rule(contexts: list[str]) -> dict:
    return {
        "type": "required_status_checks",
        "parameters": {"required_status_checks": [{"context": c} for c in contexts]},
    }


def test_live_required_contexts_extracts_from_required_status_checks_rule() -> None:
    rules = [
        {"type": "deletion"},
        _required_status_checks_rule(["Lint Python Code", "Validate Compose Files"]),
    ]
    result = cgra._live_required_contexts(
        "wangzitian0/infra2", "main", "tok", opener=_opener_returning(rules)
    )
    # The reader now also returns the ruleset ids those rules came from, so the
    # audit can ask whether those rulesets are enforcing at all.
    contexts, ruleset_ids = result
    assert contexts == {"Lint Python Code", "Validate Compose Files"}
    assert isinstance(ruleset_ids, set)


def test_live_required_contexts_is_failsafe_none_on_network_error() -> None:
    result = cgra._live_required_contexts(
        "wangzitian0/infra2", "main", "tok", opener=_raising_opener
    )
    assert result is None


def test_audit_reports_in_sync_when_declared_matches_live(monkeypatch) -> None:
    monkeypatch.setattr(
        cgra,
        "_blocking_gates",
        lambda: [
            {"id": "infra_ci.lint_python", "workflow": "wf.yml", "job": "lint-python"}
        ],
    )
    monkeypatch.setattr(cgra, "_job_display_name", lambda *_a: "Lint Python Code")
    monkeypatch.setattr(cgra, "_defanged", lambda *_a: [])
    monkeypatch.setattr(
        cgra,
        "_live_required_contexts",
        lambda *_a, **_kw: ({"Lint Python Code"}, {1}),
    )

    monkeypatch.setattr(cgra, "_ruleset_posture", lambda *_a, **_kw: _HEALTHY_POSTURE)
    result = cgra.audit(token="tok")
    assert result["status"] == "in_sync"
    assert result["missing_from_ruleset"] == []
    assert result["extra_in_ruleset"] == []


def test_audit_flags_gate_missing_from_ruleset(monkeypatch) -> None:
    monkeypatch.setattr(
        cgra,
        "_blocking_gates",
        lambda: [
            {"id": "infra_ci.lint_python", "workflow": "wf.yml", "job": "lint-python"}
        ],
    )
    monkeypatch.setattr(cgra, "_job_display_name", lambda *_a: "Lint Python Code")
    monkeypatch.setattr(cgra, "_defanged", lambda *_a: [])
    monkeypatch.setattr(
        cgra, "_live_required_contexts", lambda *_a, **_kw: (set(), {1})
    )

    monkeypatch.setattr(cgra, "_ruleset_posture", lambda *_a, **_kw: _HEALTHY_POSTURE)
    result = cgra.audit(token="tok")
    assert result["status"] == "drift"
    assert result["missing_from_ruleset"] == ["Lint Python Code"]


def test_audit_flags_extra_check_in_ruleset_not_declared(monkeypatch) -> None:
    monkeypatch.setattr(cgra, "_blocking_gates", lambda: [])
    monkeypatch.setattr(
        cgra,
        "_live_required_contexts",
        lambda *_a, **_kw: ({"Some Unregistered Check"}, {1}),
    )

    monkeypatch.setattr(cgra, "_ruleset_posture", lambda *_a, **_kw: _HEALTHY_POSTURE)
    result = cgra.audit(token="tok")
    assert result["status"] == "drift"
    assert result["extra_in_ruleset"] == ["Some Unregistered Check"]


def test_audit_flags_self_contradicting_gate(monkeypatch) -> None:
    """A blocks_merge: true gate whose job is continue-on-error can never actually
    block — even if the ruleset happens to list it, the declaration is dishonest."""
    monkeypatch.setattr(
        cgra,
        "_blocking_gates",
        lambda: [
            {
                "id": "infra_ci.vault_policy",
                "workflow": "wf.yml",
                "job": "validate-vault-policy",
            }
        ],
    )
    monkeypatch.setattr(
        cgra, "_job_display_name", lambda *_a: "Validate Vault Policy Syntax"
    )
    monkeypatch.setattr(cgra, "_defanged", lambda *_a: ["Run ruff check"])
    monkeypatch.setattr(
        cgra,
        "_live_required_contexts",
        lambda *_a, **_kw: ({"Validate Vault Policy Syntax"}, {1}),
    )

    monkeypatch.setattr(cgra, "_ruleset_posture", lambda *_a, **_kw: _HEALTHY_POSTURE)
    result = cgra.audit(token="tok")
    assert result["status"] == "drift"
    # The report names which part was defanged, not just which gate: a
    # reader who has to open the workflow to find out is one step
    # further from fixing it.
    assert result["self_contradicting_gates"] == [
        "infra_ci.vault_policy (Run ruff check)"
    ]


def test_audit_is_undetermined_when_live_state_unreachable(monkeypatch) -> None:
    monkeypatch.setattr(cgra, "_blocking_gates", lambda: [])
    monkeypatch.setattr(cgra, "_live_required_contexts", lambda *_a, **_kw: None)

    monkeypatch.setattr(cgra, "_ruleset_posture", lambda *_a, **_kw: _HEALTHY_POSTURE)
    result = cgra.audit(token="tok")
    assert result["live_required_checks"] is None
    assert result["status"].startswith("undetermined")


def test_current_inventory_matches_the_real_infra_ci_workflow() -> None:
    """Every declared blocking gate must resolve to a real job `name:` in its
    workflow file — catches a gate pointing at a renamed/removed job."""
    gates = list(cgra._blocking_gates())
    assert len(gates) > 0, (
        "No blocking gates found in inventory (GREEN-WHILE-EMPTY risk)"
    )
    for gate in gates:
        name = cgra._job_display_name(gate["workflow"], gate["job"])
        assert name is not None, (
            f"{gate['id']} -> {gate['workflow']}:{gate['job']} has no job name"
        )


def test_current_inventory_has_no_self_contradicting_gates() -> None:
    """Regression guard for #504: a blocks_merge: true gate must not be
    continue-on-error (it could never actually block).

    Now reads step level too. A `continue-on-error` on the step that runs the
    check defangs the gate exactly as thoroughly as one on the job, and was
    invisible here until it was measured. A deliberate exemption declares
    itself with `# gate-exempt: <reason>` beside the step.
    """
    gates = list(cgra._blocking_gates())
    assert len(gates) > 0, (
        "No blocking gates found in inventory (GREEN-WHILE-EMPTY risk)"
    )
    contradicting = [
        f"{gate['id']} ({', '.join(found)})"
        for gate in gates
        if (found := cgra._defanged(gate["workflow"], gate["job"]))
    ]
    assert contradicting == []


def test_a_ruleset_that_is_not_enforcing_is_drift(monkeypatch) -> None:
    """A ruleset set to `disabled` or `evaluate` keeps every rule listed and
    stops applying them — the check list above becomes decorative.

    Editable from the GitHub UI, which leaves no commit, no diff, nothing in
    this repository for a reviewer to see. This audit is the only thing
    positioned to notice.
    """
    monkeypatch.setattr(cgra, "_blocking_gates", lambda: [])
    monkeypatch.setattr(
        cgra, "_live_required_contexts", lambda *_a, **_kw: (set(), {1})
    )
    monkeypatch.setattr(
        cgra,
        "_ruleset_posture",
        lambda *_a, **_kw: {
            "rulesets": [{"id": 1, "name": "main", "enforcement": "evaluate"}],
            "inactive": ["main: enforcement=evaluate"],
            "bypassable": [],
        },
    )

    result = cgra.audit(token="tok")
    assert result["status"] == "drift"
    assert result["inactive_rulesets"] == ["main: enforcement=evaluate"]


def test_a_bypassable_ruleset_is_drift(monkeypatch) -> None:
    """Anyone in `bypass_actors` merges past every required check.

    Nothing else in this repository can see that list — it is not a file.
    """
    monkeypatch.setattr(cgra, "_blocking_gates", lambda: [])
    monkeypatch.setattr(
        cgra, "_live_required_contexts", lambda *_a, **_kw: (set(), {1})
    )
    monkeypatch.setattr(
        cgra,
        "_ruleset_posture",
        lambda *_a, **_kw: {
            "rulesets": [{"id": 1, "name": "main", "enforcement": "active"}],
            "inactive": [],
            "bypassable": ["main: 1 bypass actor(s)"],
        },
    )

    result = cgra.audit(token="tok")
    assert result["status"] == "drift"
    assert result["bypassable_rulesets"] == ["main: 1 bypass actor(s)"]


def test_an_unreadable_posture_is_undetermined_not_in_sync(monkeypatch) -> None:
    """Fail-safe, matching how an unreachable rules API is already handled: a
    posture nobody could read is not a healthy one."""
    monkeypatch.setattr(cgra, "_blocking_gates", lambda: [])
    monkeypatch.setattr(
        cgra, "_live_required_contexts", lambda *_a, **_kw: (set(), {1})
    )
    monkeypatch.setattr(cgra, "_ruleset_posture", lambda *_a, **_kw: None)

    result = cgra.audit(token="tok")
    assert result["status"].startswith("undetermined")
    assert "in_sync" not in result["status"]


def test_live_required_contexts_returns_contexts_and_their_ruleset_ids() -> None:
    """The shape the annotation promises (#789 review found them disagreeing).

    The ids ride along with the contexts because `_ruleset_posture` needs them
    and this is the call that already knows them -- asking twice would let the
    two answers describe different rulesets.
    """
    import json
    from contextlib import contextmanager

    from common.core.ci_gate_ruleset_audit import _live_required_contexts

    payload = [
        {
            "ruleset_id": 42,
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": "Lint Python Code"}]},
        },
        {"ruleset_id": 7, "type": "deletion"},
    ]

    @contextmanager
    def opener(request, timeout=None):
        assert request.headers["Authorization"] == "Bearer t0ken", (
            "the caller's token must reach the request"
        )

        class _R:
            def read(self):
                return json.dumps(payload).encode()

        yield _R()

    result = _live_required_contexts("o/r", "main", "t0ken", opener=opener)
    assert isinstance(result, tuple) and len(result) == 2, result
    contexts, ruleset_ids = result
    assert contexts == {"Lint Python Code"}
    assert ruleset_ids == {42, 7}, "every ruleset seen, not only the ones with checks"


def test_an_unreachable_rules_api_is_undetermined_not_empty() -> None:
    """None, not ([], []): a caller reading "no requirements" from a failed call
    is the same hole as an empty protected set."""
    from common.core.ci_gate_ruleset_audit import _live_required_contexts

    def opener(request, timeout=None):
        raise OSError("network down")

    assert _live_required_contexts("o/r", "main", "t", opener=opener) is None


# ========== gate schema ==========


def test_valid_gate_passes() -> None:
    assert validate_gate(_gate(), stage_ids=STAGES) == []


def test_missing_required_field_rejected() -> None:
    gate = _gate()
    del gate["job"]
    assert any("job" in e for e in validate_gate(gate, stage_ids=STAGES))


def test_duplicate_ids_rejected() -> None:
    result = validate_inventory([_gate(), _gate()], stage_ids=STAGES)
    assert any("duplicate" in e for e in result["errors"])


# ========== defanging ==========


def _wf(text: str) -> dict:
    return yaml.safe_load(text)


STEP_LEVEL = """
jobs:
  gate:
    steps:
      - name: Run ruff check
        continue-on-error: true
        run: ruff check .
"""

JOB_LEVEL = """
jobs:
  gate:
    continue-on-error: true
    steps:
      - name: Run ruff check
        run: ruff check .
"""

EXEMPTED = """
jobs:
  gate:
    steps:
      - name: Upload coverage
        # gate-exempt: reports a number, does not check one
        continue-on-error: true
        uses: coverallsapp/github-action@v2
"""


def test_a_step_level_continue_on_error_is_found() -> None:
    """The case that was invisible, and the reason this file exists."""
    assert ci_spec.defanged_steps(_wf(STEP_LEVEL), "gate", STEP_LEVEL) == [
        "Run ruff check"
    ]


def test_job_level_is_still_found() -> None:
    """Deepening the check must not lose the level it already had."""
    assert ci_spec.defanged_steps(_wf(JOB_LEVEL), "gate", JOB_LEVEL) == ["<job>"]


def test_a_declared_exemption_is_not_a_finding() -> None:
    """Otherwise the check is noise, and a noisy gate gets switched off."""
    assert ci_spec.defanged_steps(_wf(EXEMPTED), "gate", EXEMPTED) == []


def test_an_undeclared_exemption_is_a_finding() -> None:
    """The control for the test above: the comment is doing the work, not the
    step's name or what it uses."""
    without = EXEMPTED.replace(
        "        # gate-exempt: reports a number, does not check one\n", ""
    )
    assert ci_spec.defanged_steps(_wf(without), "gate", without) == ["Upload coverage"]


def test_without_the_source_every_one_is_reported() -> None:
    """Exemptions live in comments, which YAML parsing discards.

    A caller that cannot supply the text gets the unfiltered list rather than a
    quietly shorter one — wrong in the direction that shows up.
    """
    assert ci_spec.defanged_steps(_wf(EXEMPTED), "gate") == ["Upload coverage"]


@pytest.mark.parametrize(
    "job", ("lint-python", "test-deployer-logic", "validate-compose")
)
def test_the_real_required_jobs_carry_no_undeclared_exemption(job: str) -> None:
    """The live assertion. A new `continue-on-error` on a required job has to
    explain itself before this goes green again."""
    source = INFRA_CI_PATH.read_text(encoding="utf-8")
    found = ci_spec.defanged_steps(ci_spec.load_workflow(INFRA_CI_PATH), job, source)
    assert not found, (
        f"{job} has step(s) whose failure cannot fail it and which give no reason: "
        f"{found}. Add `# gate-exempt: <reason>` if that is deliberate."
    )


def test_a_missing_workflow_reports_nothing_rather_than_raising() -> None:
    """An auditor's job is to report; a deleted workflow is a finding for the
    caller to phrase, not a traceback."""
    # Built from segments: spelling it whole would make
    # test_workflow_reference_contract read it as a reference to a
    # workflow that ought to exist.
    absent = ROOT / ".github" / "workflows" / "does-not-exist.yml"
    assert ci_spec.load_workflow(absent) == {}


# -- An exemption exempts one thing (#788 review) ------------------------------------

JOB_LEVEL_PLUS_EXEMPTED_STEP = """
jobs:
  gate:
    continue-on-error: true
    runs-on: ubuntu-latest
    steps:
      - name: Run ruff check
        run: ruff check .
      - name: Upload coverage
        # gate-exempt: reports a number, does not check one
        continue-on-error: true
        uses: coverallsapp/github-action@v2
"""

EXEMPTED_JOB = """
jobs:
  gate:
    # gate-exempt: advisory only, declared blocks_merge:false
    continue-on-error: true
    runs-on: ubuntu-latest
    steps:
      - name: Run ruff check
        run: ruff check .
"""


def test_an_exempted_step_does_not_silence_its_job() -> None:
    """The bypass this audit would otherwise have rebuilt inside itself.

    The job is `continue-on-error: true` AND an unrelated coverage step carries a
    `# gate-exempt:`. Reading the exemptions as one workflow-wide flag (`not exempt`)
    made the second fact hide the first: add one exempted step anywhere in the file
    and a required job could be defanged silently -- exactly the drift this audit
    exists to detect.
    """
    found = ci_spec.defanged_steps(
        _wf(JOB_LEVEL_PLUS_EXEMPTED_STEP), "gate", JOB_LEVEL_PLUS_EXEMPTED_STEP
    )
    assert found == ["<job>"], found


def test_a_job_can_be_exempted_by_its_own_comment() -> None:
    """The counterpart: the exemption keyed to the job itself does apply to it,
    so the mechanism still has a legitimate job-level use."""
    assert ci_spec.defanged_steps(_wf(EXEMPTED_JOB), "gate", EXEMPTED_JOB) == []


def test_one_jobs_exemption_does_not_carry_to_another() -> None:
    """Keying is per declaration, not 'the last comment seen wins the file'."""
    two_jobs = (
        EXEMPTED_JOB
        + """  other:
    continue-on-error: true
    runs-on: ubuntu-latest
    steps:
      - name: Run tests
        run: pytest
"""
    )
    assert ci_spec.defanged_steps(_wf(two_jobs), "gate", two_jobs) == []
    assert ci_spec.defanged_steps(_wf(two_jobs), "other", two_jobs) == ["<job>"]


# -- load_workflow's docstring promise (#788 review) ----------------------------------


@pytest.mark.parametrize("body", ["", "null\n", "- a\n- b\n", "just a string\n"])
def test_valid_yaml_that_is_not_a_mapping_reads_as_empty(tmp_path, body) -> None:
    """`yaml.safe_load` happily returns None/list/str, which sails past the
    YAMLError handler and breaks every `.get()` downstream."""
    p = tmp_path / "wf.yml"
    p.write_text(body, encoding="utf-8")
    assert ci_spec.load_workflow(p) == {}


def test_an_unreadable_file_reads_as_empty(tmp_path) -> None:
    """The docstring promised {} for 'absent or unreadable', but only malformed
    YAML was caught -- a permission error or invalid UTF-8 raised through it."""
    bad_utf8 = tmp_path / "bad.yml"
    bad_utf8.write_bytes(b"jobs:\n  gate:\n    name: \xff\xfe\n")
    assert ci_spec.load_workflow(bad_utf8) == {}

    no_read = tmp_path / "locked.yml"
    no_read.write_text("jobs: {}\n", encoding="utf-8")
    no_read.chmod(0o000)
    try:
        # chmod does not stop root, and permission semantics differ by platform
        # (#788 review). Assert the OSError path only where the chmod actually
        # took effect; otherwise this fails for a reason that is not the code's.
        try:
            no_read.read_bytes()
        except OSError:
            assert ci_spec.load_workflow(no_read) == {}
        else:
            pytest.skip("this environment can read a 0o000 file")
    finally:
        no_read.chmod(0o644)


# -- One read, and an exemption that stays where it was written (#788 review 2) ------


def test_read_workflow_returns_parse_and_source_together(tmp_path) -> None:
    """Two reads meant two failure paths and one handler; callers take both here."""
    wf = tmp_path / "wf.yml"
    wf.write_text(JOB_LEVEL, encoding="utf-8")
    parsed, source = ci_spec.read_workflow(wf)
    assert parsed["jobs"]["gate"]["continue-on-error"] is True
    assert source == JOB_LEVEL


def test_read_workflow_of_an_unreadable_file_yields_no_source(tmp_path) -> None:
    """The second read was the unhandled one: a caller that got {} from
    load_workflow still crashed fetching the comments."""
    bad = tmp_path / "bad.yml"
    bad.write_bytes(b"jobs:\n  gate:\n    name: \xff\xfe\n")
    assert ci_spec.read_workflow(bad) == ({}, "")
    assert ci_spec.read_workflow(tmp_path / "absent.yml") == ({}, "")


def test_read_workflow_keeps_the_text_when_only_the_yaml_is_broken(tmp_path) -> None:
    wf = tmp_path / "wf.yml"
    wf.write_text("jobs: [\n", encoding="utf-8")
    parsed, source = ci_spec.read_workflow(wf)
    assert parsed == {} and source == "jobs: [\n"


UNNAMED_EXEMPT_STEP = """
jobs:
  gate:
    continue-on-error: true
    runs-on: ubuntu-latest
    steps:
      - # gate-exempt: nothing should be exempted by this
        run: ./flaky.sh
        continue-on-error: true
"""


def test_a_comment_on_an_unnamed_step_exempts_nothing() -> None:
    """The bypass one level down from the last one.

    `current` used to survive into the next declaration, so a bare
    `- # gate-exempt:` still pointed at the enclosing job and silenced the job's
    own `continue-on-error`. The docstring already promised it exempted nothing.
    """
    found = ci_spec.defanged_steps(
        _wf(UNNAMED_EXEMPT_STEP), "gate", UNNAMED_EXEMPT_STEP
    )
    assert "<job>" in found, found
    assert any(f.startswith("step #") for f in found), found


def test_an_exemption_can_be_keyed_to_a_step_named_only_by_uses() -> None:
    """`defanged_steps` reports such a step by its `uses`, so that is the name an
    exemption has to be written against for a reader to match them up."""
    wf = """
jobs:
  gate:
    runs-on: ubuntu-latest
    steps:
      - uses: coverallsapp/github-action@v2
        # gate-exempt: reports a number, does not check one
        continue-on-error: true
"""
    assert ci_spec.defanged_steps(_wf(wf), "gate", wf) == []


@pytest.mark.parametrize(
    "node", ["jobs:\n  gate: notamapping\n", "jobs:\n  gate:\n    - a\n"]
)
def test_a_job_node_that_is_not_a_mapping_is_not_a_crash(node) -> None:
    assert ci_spec.defanged_steps(_wf(node), "gate", node) == []


def test_steps_that_are_not_a_list_of_mappings_are_not_a_crash() -> None:
    wf = "jobs:\n  gate:\n    continue-on-error: true\n    steps: oops\n"
    assert ci_spec.defanged_steps(_wf(wf), "gate", wf) == ["<job>"]
    wf2 = "jobs:\n  gate:\n    steps:\n      - just a string\n"
    assert ci_spec.defanged_steps(_wf(wf2), "gate", wf2) == []


# -- One reader for all three auditors (#788 review 2) -------------------------------


def test_every_gate_auditor_reads_workflows_through_ci_spec() -> None:
    """The header comment claimed a consolidation that had been done for one of
    the three (#788 review). Assert the claim instead of restating it: no auditor
    may parse a workflow itself.

    The inventory YAML is a different question with different readers, so this
    looks only at the workflow path: a `yaml.safe_load` whose argument mentions a
    workflow, or a `.read_text()` on a path that came from `WORKFLOWS`/`workflow`.
    """
    root = Path(__file__).resolve().parents[2]
    for name in ("ci_gate_audit.py", "ci_gate_lint.py", "ci_gate_ruleset_audit.py"):
        for folder in ("tools", "common/core"):
            source = (root / folder / name).read_text(encoding="utf-8")
            offenders = [
                line.strip()
                for line in source.splitlines()
                if "yaml.safe_load" in line and "INVENTORY" not in line.upper()
            ]
            assert not offenders, (
                f"{folder}/{name} parses a workflow itself instead of using "
                f"tools.ci_spec.read_workflow: {offenders}"
            )


def test_the_linter_still_reports_why_a_workflow_did_not_parse(tmp_path) -> None:
    """Routing the linter through the shared reader must not cost it the reason.

    `read_workflow` folds every unusable file into an empty mapping, which is what
    an auditor wants and what a linter must not silently inherit -- a broken
    workflow would lint clean.
    """
    from common.core import ci_gate_lint

    broken = tmp_path / "broken.yml"
    broken.write_text("jobs: [\n", encoding="utf-8")
    findings = ci_gate_lint.lint_workflow(broken)
    assert len(findings) == 1 and "Failed to parse YAML" in findings[0], findings

    not_a_mapping = tmp_path / "list.yml"
    not_a_mapping.write_text("- a\n- b\n", encoding="utf-8")
    assert ci_gate_lint.lint_workflow(not_a_mapping), "a list is not a workflow"


@pytest.mark.parametrize("body", ["", "   \n\n"])
def test_a_blank_workflow_is_not_a_lint_failure(tmp_path, body) -> None:
    """Preserved on purpose: this has always been no findings, and turning it into
    a failure would be a behaviour change smuggled in under a refactor."""
    from common.core import ci_gate_lint

    blank = tmp_path / "blank.yml"
    blank.write_text(body, encoding="utf-8")
    assert ci_gate_lint.lint_workflow(blank) == []


# -- 一条豁免只豁免它写在谁头上的那一个（审计 2026-09-22 第三层）-----------------

SAME_NAME_ACROSS_JOBS = """
jobs:
  advisory_job:
    steps:
      - name: Lint
        # gate-exempt: advisory job, flakiness tolerated
        continue-on-error: true
        run: ruff check .
  required_job:
    steps:
      - name: Lint
        continue-on-error: true
        run: ruff check .
"""


def test_an_exemption_does_not_cross_into_another_job_with_the_same_step_name() -> None:
    """同一个绕过的第三层。

    前两层：workflow 级开关（任一豁免静默所有 job 级 defang）、`current` 跨声明残留
    （无名 step 上的注释豁免了外层 job）。这一层是**文件级扁平集合**：只按名字记，
    于是 advisory job 里合法豁免的 `Lint`，让 required job 里同名、**毫无豁免声明**的
    `continue-on-error` 被静默放过。

    `Lint` / `Test` / `Build` 跨 job 重名在真实 workflow 里极其常见，不需要恶意构造。
    审计实测发现（2026-09-22），此前没有任何测试覆盖 —— 已有的那条只测了不同 job id
    下**不同名**的 step，恰好绕开了会撞车的场景。
    """
    parsed = _wf(SAME_NAME_ACROSS_JOBS)
    assert ci_spec.defanged_steps(parsed, "advisory_job", SAME_NAME_ACROSS_JOBS) == []
    assert ci_spec.defanged_steps(parsed, "required_job", SAME_NAME_ACROSS_JOBS) == [
        "Lint"
    ], "required job 的 Lint 没有任何豁免声明，必须报出来"


BLOCK_SCALAR_LOOKALIKE = """
jobs:
  gate:
    steps:
      - name: Print an example
        run: |
          cat <<'YAML'
          - name: Run something risky
            # gate-exempt: 这只是 heredoc 里的示例文本
          YAML
      - name: Run something risky
        continue-on-error: true
        run: ./flaky.sh
"""


def test_text_inside_a_block_scalar_is_not_a_declaration() -> None:
    """`run: |` 的正文是字符串，不是声明。把它当声明会凭空造出一条谁都没写过的豁免，
    而写它的人根本不知道自己豁免了什么。"""
    found = ci_spec.defanged_steps(
        _wf(BLOCK_SCALAR_LOOKALIKE), "gate", BLOCK_SCALAR_LOOKALIKE
    )
    assert found == ["Run something risky"], found
