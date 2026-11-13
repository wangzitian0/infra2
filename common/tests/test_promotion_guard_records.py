"""#1646: the production promotion guard reads stage records, in the `pre` job.

A production promotion needs the post record of the staging soak of the same tag (a tag-push
run, the tag on main), the post record of the main CI run of the tag commit, and a green
`staging_gate` of every App release since the last production marker. A missing, failed,
expired, other-tag, dispatch-triggered, off-main or wrong-head record makes `pre` refuse
with the failing check id, before the owner is asked. Each case drives the real `main` with
the fake GitHub of test_stage_records.py.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from common.deploy import promotion_guard as guard
import tools.reconcile_iac_inputs as reconcile
from common.deploy import stage_record as writer
from common.deploy import stage_record_reader as stage_records
from common.tests.test_stage_records import FakeGitHub, _record

ROOT = Path(__file__).resolve().parents[2]
CI = ".github/workflows/infra-ci.yml"
TAG = "v3.1.23"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    """A repository whose tag v3.1.23 is the commit that the fake GitHub calls the tag commit."""
    root = tmp_path / "repo"
    (root / "docs" / "ssot").mkdir(parents=True)
    (root / "harness").mkdir()
    (root / "docs" / "ssot" / "deploy-dependencies.yaml").write_text(
        (ROOT / "docs/ssot/deploy-dependencies.yaml").read_text(encoding="utf-8")
    )
    (root / "harness" / "repos.yaml").write_text(
        "schema_version: 1\nrepositories:\n"
        "  - id: finance-report\n    role: external-application\n    release_tag: v0.2.61\n",
        encoding="utf-8",
    )
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "release")
    _git(root, "tag", TAG)
    return root


@pytest.fixture
def world(repo, monkeypatch, tmp_path):
    """The fake GitHub, wired into the guard, and no deploy call allowed."""
    gh = FakeGitHub()
    gh.commit = _git(repo, "rev-parse", f"{TAG}^{{commit}}")
    monkeypatch.setattr(
        guard,
        "read_record",
        lambda *a, **k: stage_records.read_record(*a, opener=gh, **k),
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_RUN_ID", "9")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("STAGE_RECORD_DIR", str(tmp_path / "records"))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(reconcile, "assert_after_on_main", lambda *_a, **_k: None)
    # the App pins have their own tests; this file tests the upstream record check
    monkeypatch.setattr(
        reconcile, "assert_app_version_ref_on_main", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        reconcile, "assert_app_version_ref_matches_train", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        reconcile, "assert_production_marker_advances", lambda *_a, **_k: "v3.1.22"
    )
    monkeypatch.setattr(
        reconcile,
        "run_deploy_commands",
        lambda *_a, **_k: pytest.fail("pre must not deploy"),
    )
    gh.repo = repo
    gh.records = tmp_path / "records"
    return gh


def _green(
    world: FakeGitHub, *, staging: dict | None = None, ci: dict | None = None
) -> None:
    """The two upstream records of a green promotion of v3.1.23."""
    world.add(_record(subject=f"tag:{TAG}", run=1), run=1, **(staging or {}))
    ci_record = _record(env=writer.CI_ENV, subject=f"sha:{world.commit}", run=2)
    world.add(ci_record, run=2, path=CI, event="push", **(ci or {}))


def _pre(world: FakeGitHub, *extra: str) -> int:
    return reconcile.main(
        [
            "--repo-root", str(world.repo), "--phase", "pre",
            "--before", "v3.1.22", "--after", TAG, "--promote-prod",
            "--changed-file", "platform/12.alerting/compose.yaml", *extra,
        ]
    )  # fmt: skip


def _pre_record(world: FakeGitHub) -> dict:
    return json.loads(next(world.records.glob("*-pre-*")).read_text())


def test_a_green_staging_post_and_a_green_main_ci_pass_pre(world) -> None:
    _green(world)
    assert _pre(world) == 0
    checks = {c["name"]: c["ok"] for c in _pre_record(world)["checks"]}
    assert checks["upstream_post"] is True


@pytest.mark.parametrize(
    ("staging", "needle"),
    [
        (None, "no artifact of that name"),  # missing: set below
        ({"expired": True}, "the artifact is expired"),
        ({"event": "workflow_dispatch", "head_branch": "main"}, "not 'push'"),
        ({"head_sha": "e" * 40}, "did not build the commit"),
    ],
    ids=["missing", "expired", "dispatch-triggered", "wrong-head-sha"],
)
def test_pre_refuses_a_staging_post_record_that_does_not_count(
    world, capsys, staging, needle
) -> None:
    if staging is None:  # no staging record at all; the CI record is green
        ci_record = _record(env=writer.CI_ENV, subject=f"sha:{world.commit}", run=2)
        world.add(ci_record, run=2, path=CI, event="push")
    else:
        _green(world, staging=staging)
    with pytest.raises(SystemExit) as refused:
        _pre(world)
    assert "upstream_post refused: staging soak" in str(refused.value)
    assert needle in capsys.readouterr().out
    pre = _pre_record(world)
    assert pre["verdict"] == "fail"
    assert [c["name"] for c in pre["checks"] if not c["ok"]] == ["upstream_post"]


def test_pre_refuses_a_failed_staging_post_and_names_the_failing_check_id(
    world, capsys
) -> None:
    red = _record(subject=f"tag:{TAG}", run=1)
    red["checks"] = [
        {
            "name": "platform/todo:staging_gate",
            "ok": True,
            "detail": "",
            "seconds": 1.0,
        },
        {
            "name": "truealpha/app:staging_gate",
            "ok": False,
            "detail": "x",
            "seconds": 1.0,
        },
    ]
    red["verdict"] = "fail"
    world.add(red, run=1)
    world.add(
        _record(env=writer.CI_ENV, subject=f"sha:{world.commit}", run=2),
        run=2,
        path=CI,
        event="push",
    )
    with pytest.raises(SystemExit) as refused:
        _pre(world)
    assert "failed check truealpha/app:staging_gate" in str(refused.value)
    assert "platform/todo" not in str(refused.value)


def test_pre_refuses_the_staging_post_record_of_another_tag(world) -> None:
    world.add(_record(subject="tag:v9.9.9", run=1), run=1)
    world.add(
        _record(env=writer.CI_ENV, subject=f"sha:{world.commit}", run=2),
        run=2,
        path=CI,
        event="push",
    )
    with pytest.raises(SystemExit) as refused:
        _pre(world)
    assert "upstream_post refused: staging soak" in str(refused.value)


def test_pre_refuses_a_tag_outside_main(world, capsys) -> None:
    """A `v*` tag on a commit outside main runs that commit's own workflow, so its green
    record means nothing: the compare call must say `behind` or `identical`."""
    _green(world)
    world.compare_status = "diverged"
    with pytest.raises(SystemExit):
        _pre(world)
    assert "is not on main (compare status 'diverged')" in capsys.readouterr().out


@pytest.mark.parametrize("status", ["behind", "identical"])
def test_pre_accepts_a_tag_that_is_behind_or_identical_to_main(world, status) -> None:
    _green(world)
    world.compare_status = status
    assert _pre(world) == 0


def test_pre_refuses_a_failed_or_missing_main_ci_record(world, capsys) -> None:
    world.add(_record(subject=f"tag:{TAG}", run=1), run=1)
    with pytest.raises(SystemExit) as missing:
        _pre(world)
    assert "main CI" in str(missing.value)
    red = _record(env=writer.CI_ENV, subject=f"sha:{world.commit}", run=2)
    red["checks"] = [{"name": "unit", "ok": False, "detail": "", "seconds": 1.0}]
    world.add(red, run=2, path=CI, event="push")
    with pytest.raises(SystemExit):
        _pre(world)
    assert "failed check unit" in capsys.readouterr().out


def test_pre_does_not_pass_when_github_cannot_be_read(world, capsys) -> None:
    _green(world)
    world.fail_on = "/actions/artifacts"
    with pytest.raises(SystemExit) as refused:
        _pre(world)
    assert "cannot read" in str(refused.value)
    assert _pre_record(world)["verdict"] == "fail"


def test_pre_without_a_github_token_refuses(world, monkeypatch) -> None:
    _green(world)
    monkeypatch.delenv("GITHUB_TOKEN")
    with pytest.raises(SystemExit, match="GITHUB_REPOSITORY and GITHUB_TOKEN"):
        _pre(world)


def test_the_guard_runs_for_a_promotion_that_deploys_nothing(world) -> None:
    """A promotion whose change set has no deploy command still needs the green records."""
    with pytest.raises(SystemExit):
        reconcile.main(
            [
                "--repo-root",
                str(world.repo),
                "--phase",
                "pre",
                "--before",
                "v3.1.22",
                "--after",
                TAG,
                "--promote-prod",
                "--changed-file",
                "docs/README.md",
            ]  # fmt: skip
        )


def test_a_staging_pre_and_a_dry_run_read_no_record(world, monkeypatch) -> None:
    monkeypatch.setattr(
        guard,
        "read_record",
        lambda *_a, **_k: pytest.fail("only a production promotion reads records"),
    )
    assert (
        reconcile.main(
            [
                "--repo-root",
                str(world.repo),
                "--phase",
                "pre",
                "--env",
                "staging",
                "--after",
                TAG,
                "--changed-file",
                "platform/12.alerting/compose.yaml",
            ]  # fmt: skip
        )
        == 0
    )
    assert _pre(world, "--dry-run") == 0


# -- the App releases since the last production marker (#1563) ---------------------------


def _app_post(tag: str, version: str, *, ok: bool = True, run: int = 1) -> dict:
    record = _record(subject=f"tag:{tag}", run=run)
    record["checks"] = [
        {
            "name": "finance_report/app:staging_gate",
            "ok": ok,
            "detail": f"[{version}] journey",
            "seconds": 1.0,
        }
    ]
    return record


def test_pre_requires_a_green_staging_gate_of_each_app_release_since_the_marker(
    world, monkeypatch
) -> None:
    """The release v3.1.22 -> v3.1.23 carries finance_report v0.2.61. Its `staging_gate` must
    be green, at that version, in a post record of a tag since the marker."""
    _green(world)
    _git(world.repo, "tag", "v3.1.22", "HEAD")
    argv = [
        "--repo-root", str(world.repo), "--phase", "pre", "--before", "v3.1.22",
        "--after", TAG, "--promote-prod", "--changed-file", "harness/repos.yaml",
    ]  # fmt: skip
    # the staging record of v3.1.23 has no staging_gate of finance_report
    with pytest.raises(SystemExit) as missing:
        reconcile.main(argv)
    assert "missing staging_gate for finance_report/app@v0.2.61" in str(missing.value)


def test_pre_passes_when_the_app_release_has_a_green_staging_gate(world) -> None:
    world.add(_app_post(TAG, "v0.2.61"), run=1)
    world.add(
        _record(env=writer.CI_ENV, subject=f"sha:{world.commit}", run=2),
        run=2,
        path=CI,
        event="push",
    )
    _git(world.repo, "tag", "v3.1.22", "HEAD")
    assert (
        reconcile.main(
            [
                "--repo-root",
                str(world.repo),
                "--phase",
                "pre",
                "--before",
                "v3.1.22",
                "--after",
                TAG,
                "--promote-prod",
                "--changed-file",
                "harness/repos.yaml",
            ]  # fmt: skip
        )
        == 0
    )


def test_pre_refuses_a_red_staging_gate_of_an_app_release_at_that_version(
    world,
) -> None:
    red = _app_post(TAG, "v0.2.61", ok=False)
    world.add(red, run=1)
    world.add(
        _record(env=writer.CI_ENV, subject=f"sha:{world.commit}", run=2),
        run=2,
        path=CI,
        event="push",
    )
    _git(world.repo, "tag", "v3.1.22", "HEAD")
    with pytest.raises(SystemExit) as refused:
        reconcile.main(
            [
                "--repo-root",
                str(world.repo),
                "--phase",
                "pre",
                "--before",
                "v3.1.22",
                "--after",
                TAG,
                "--promote-prod",
                "--changed-file",
                "harness/repos.yaml",
            ]  # fmt: skip
        )
    assert "failed check finance_report/app:staging_gate" in str(refused.value)


def _window_run(world: FakeGitHub, *, red: bool) -> list[str]:
    """v3.1.22 is the marker; v3.1.23 carries the App release; v3.1.24 is promoted."""
    for tag in ("v3.1.22", "v3.1.24"):
        _git(world.repo, "tag", tag, "HEAD")
    world.add(_record(subject="tag:v3.1.24", run=1), run=1)  # green, no App gate in it
    world.add(_app_post("v3.1.23", "v0.2.61", ok=not red, run=3), run=3)
    world.add(
        _record(env=writer.CI_ENV, subject=f"sha:{world.commit}", run=2),
        run=2,
        path=CI,
        event="push",
    )
    return [
        "--repo-root", str(world.repo), "--phase", "pre", "--before", "v3.1.22",
        "--after", "v3.1.24", "--promote-prod", "--changed-file", "harness/repos.yaml",
    ]  # fmt: skip


def test_the_window_since_the_marker_supplies_the_staging_gate_of_an_earlier_tag(
    world,
) -> None:
    """The App release of v3.1.23 was gated by the soak of v3.1.23; v3.1.24 is promoted."""
    assert reconcile.main(_window_run(world, red=False)) == 0


def test_a_red_staging_gate_of_an_earlier_tag_leaves_the_release_without_evidence(
    world,
) -> None:
    """A red soak is not evidence (#1563): the App release since the marker is missing."""
    with pytest.raises(SystemExit) as refused:
        reconcile.main(_window_run(world, red=True))
    assert "missing staging_gate for finance_report/app@v0.2.61" in str(refused.value)


# -- the run listing is gone --------------------------------------------------------------


def test_guard_lists_no_runs() -> None:
    """AC4 (#1646): no code of the release train lists workflow runs. The guard reads stage
    records. `git grep -nE "actions/workflows/.*/runs|look_up_soak" -- tools common/deploy`
    prints nothing."""
    pattern = re.compile(r"actions/workflows/.*/runs|look_up_soak")
    files = [
        *sorted((ROOT / "tools").rglob("*.py")),
        *sorted((ROOT / "common" / "deploy").rglob("*.py")),
    ]
    assert len(files) > 20, "the scan reads nothing"
    hits = [
        f"{path.relative_to(ROOT)}:{n}"
        for path in files
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert hits == []


def test_the_guard_reads_only_the_artifact_and_run_endpoints() -> None:
    """The reader asks for artifacts, one run by id, one commit and one compare: no listing
    of the runs of a workflow (eventually consistent, #970)."""
    source = (ROOT / "common/deploy/stage_record_reader.py").read_text(encoding="utf-8")
    endpoints = set(re.findall(r'f"(/repos/\{repo\}/[a-z]+(?:/[a-z\{\}_]+)*)', source))
    assert endpoints == {
        "/repos/{repo}/actions/runs/{run_id}",
        "/repos/{repo}/commits/{ident}",
        "/repos/{repo}/actions/artifacts",
        "/repos/{repo}/compare/{sha}",
    }, endpoints


def test_the_guard_cli_calls_the_common_function_and_exits_nonzero_on_a_refusal(
    monkeypatch, capsys
) -> None:
    import tools.promotion_soak_guard as cli

    seen: dict[str, object] = {}

    def fake(after, repo_root, *, before, required, releases):
        seen.update(
            after=after, before=before, required=dict(required), releases=releases()
        )
        raise SystemExit("::error::upstream_post refused: x")

    monkeypatch.setattr(cli, "require_upstream_post", fake)
    argv = ["--after", "v1.0.2", "--before", "v1.0.0", "--release", "v1.0.1",
            "--require", "finance_report/app=v0.2.61"]  # fmt: skip
    assert cli.main(argv) == 1
    assert seen == {
        "after": "v1.0.2",
        "before": "v1.0.0",
        "required": {"finance_report/app": "v0.2.61"},
        "releases": ["v1.0.1"],
    }
    assert "upstream_post refused" in capsys.readouterr().err
