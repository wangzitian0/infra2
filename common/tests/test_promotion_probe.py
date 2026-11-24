"""#1830: a read-only probe asks the real promotion guard "would production promote now?".

The probe builds the inputs of the ``pre`` job for the next production candidate and calls
``require_upstream_post`` on the stage records. These tests replay the v3.1.159 refusal:
the staging soak is green, yet no ``truealpha/app:staging_gate`` record names v0.2.137.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from common.deploy import promotion_guard as guard
from common.deploy import promotion_probe as probe
from common.deploy import stage_record as writer
from common.deploy import stage_record_reader as stage_records
from common.tests.test_stage_records import FakeGitHub, _record

ROOT = Path(__file__).resolve().parents[2]
CI = ".github/workflows/infra-ci.yml"
ENV = {"GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "tok"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _pins(tag: str) -> str:
    return (
        "schema_version: 1\nrepositories:\n"
        f"  - id: truealpha\n    role: external-application\n    release_tag: {tag}\n"
    )


@pytest.fixture
def repo(tmp_path) -> Path:
    """production/v3.1.22 pins truealpha v0.2.136; release v3.1.23 pins v0.2.137."""
    root = tmp_path / "repo"
    (root / "docs" / "ssot").mkdir(parents=True)
    (root / "harness").mkdir()
    (root / "docs/ssot/deploy-dependencies.yaml").write_text(
        (ROOT / "docs/ssot/deploy-dependencies.yaml").read_text(encoding="utf-8")
    )
    (root / "harness/repos.yaml").write_text(_pins("v0.2.136"), encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "r22")
    _git(root, "tag", "v3.1.22")
    _git(root, "tag", "production/v3.1.22")
    (root / "harness/repos.yaml").write_text(_pins("v0.2.137"), encoding="utf-8")
    _git(root, "commit", "-qam", "r23")
    _git(root, "tag", "v3.1.23")
    return root


@pytest.fixture
def world(repo, monkeypatch) -> FakeGitHub:
    gh = FakeGitHub()
    gh.commit = _git(repo, "rev-parse", "v3.1.23^{commit}")
    monkeypatch.setattr(
        guard,
        "read_record",
        lambda *a, **k: stage_records.read_record(*a, opener=gh, **k),
    )
    ci = _record(env=writer.CI_ENV, subject=f"sha:{gh.commit}", run=2)
    gh.add(ci, run=2, path=CI, event="push")
    return gh


def _soak(world: FakeGitHub, *, gate: str | None) -> None:
    record = _record(subject="tag:v3.1.23", run=1)
    if gate is not None:
        record["checks"].append(
            {
                "name": "truealpha/app:staging_gate",
                "ok": True,
                "detail": gate,
                "seconds": 1.0,
            }
        )
    world.add(record, run=1)


def test_the_candidate_is_the_newest_release_newer_than_the_marker(repo) -> None:
    assert probe.next_candidate(repo) == "v3.1.23"
    _git(repo, "tag", "production/v3.1.23")
    assert probe.next_candidate(repo) == ""


def test_v3_1_159_replay_is_refused_for_the_missing_staging_gate(world, repo) -> None:
    """The soak is green; no staging_gate record names truealpha/app@v0.2.137."""
    _soak(world, gate=None)
    verdict = probe.probe("v3.1.23", repo, env=ENV)
    assert not verdict.ok
    assert verdict.before == "v3.1.22"
    assert verdict.required == {"truealpha/app": "v0.2.137"}
    assert "missing staging_gate for truealpha/app@v0.2.137" in verdict.detail
    assert "::error::" not in verdict.detail


def test_a_green_staging_gate_at_the_pinned_version_passes(world, repo) -> None:
    _soak(world, gate="[v0.2.137] walk ok")
    assert probe.probe("v3.1.23", repo, env=ENV).ok


def test_a_staging_gate_of_another_version_does_not_count(world, repo) -> None:
    _soak(world, gate="[v0.2.136] walk ok")
    assert not probe.probe("v3.1.23", repo, env=ENV).ok


def test_the_probe_passes_the_inputs_of_the_pre_job_to_the_guard(repo) -> None:
    seen = {}

    def fake(after, root, **kw):
        seen.update(after=after, **kw)

    probe.probe("v3.1.23", repo, env=ENV, guard=fake)
    assert seen["after"] == "v3.1.23"
    assert seen["before"] == "v3.1.22"
    assert seen["required"] == {"truealpha/app": "v0.2.137"}
    assert seen["env"] == ENV


def test_main_exits_1_on_a_refusal_and_0_when_report_only(
    world, repo, monkeypatch, capsys
) -> None:
    _soak(world, gate=None)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    assert probe.main([], root=repo) == 1
    assert "would be refused" in capsys.readouterr().out
    assert probe.main(["--report-only"], root=repo) == 0
    assert "::warning::promotion probe" in capsys.readouterr().out


def test_main_says_nothing_to_promote_without_a_newer_release(repo, capsys) -> None:
    _git(repo, "tag", "production/v3.1.23")
    assert probe.main([], root=repo) == 0
    assert "no release tag is newer" in capsys.readouterr().out


def test_only_if_changed_skips_unless_a_guard_file_changed(
    world, repo, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    assert probe.main(["--only-if-changed", "v3.1.22..v3.1.23"], root=repo) == 0
    assert "skipped" in capsys.readouterr().out
    (repo / "common/deploy").mkdir(parents=True)
    (repo / "common/deploy/promotion_guard.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "touch the guard")
    _git(repo, "tag", "touch")
    # a guard file differs: the probe runs, finds no staging record, and refuses
    args = ["--only-if-changed", "v3.1.23..touch", "--after", "v3.1.23"]
    assert probe.main(args, root=repo) == 1


@pytest.mark.parametrize("path", probe.WATCHED_PATHS)
def test_each_watched_path_exists_and_triggers_the_probe(path) -> None:
    assert (ROOT / path).is_file()
    assert probe.touches_guard(["docs/x.md", path])
    assert not probe.touches_guard(["docs/x.md"])


def test_workflows_run_the_probe_on_guard_prs_and_on_a_report_only_schedule() -> None:
    ci = yaml.safe_load((ROOT / CI).read_text())["jobs"]["promotion-probe"]
    run = next(s["run"] for s in ci["steps"] if "promotion_probe" in s.get("run", ""))
    assert "--only-if-changed" in run and "--report-only" not in run
    assert ci["permissions"]["actions"] == "read"
    ops = yaml.safe_load((ROOT / ".github/workflows/ops-checks.yml").read_text())
    step = next(
        s
        for s in ops["jobs"]["digest"]["steps"]
        if "promotion_probe" in s.get("run", "")
    )
    assert "--report-only" in step["run"]


def test_the_watched_paths_are_exactly_the_three_guard_files() -> None:
    assert probe.WATCHED_PATHS == (
        "common/deploy/promotion_guard.py",
        "common/deploy/stage_record_reader.py",
        "common/deploy/reconcile_iac_inputs.py",
    )


def test_a_crash_while_building_the_inputs_still_exits_0_when_report_only(
    repo, monkeypatch, capsys
) -> None:
    def boom(*_a, **_k):
        raise SystemExit("::error::invalid production marker(s): production/vx")

    monkeypatch.setattr(probe, "probe", boom)
    assert probe.main(["--after", "v3.1.23", "--report-only"], root=repo) == 0
    assert "could not build the promotion" in capsys.readouterr().out
    assert probe.main(["--after", "v3.1.23"], root=repo) == 1


def test_report_only_exits_0_when_the_candidate_search_itself_crashes(
    repo, monkeypatch, capsys
) -> None:
    def boom(_root):
        raise subprocess.CalledProcessError(128, "git tag")

    monkeypatch.setattr(probe, "next_candidate", boom)
    assert probe.main(["--report-only"], root=repo) == 0
    assert "::warning::promotion probe: crashed" in capsys.readouterr().out
    with pytest.raises(subprocess.CalledProcessError):
        probe.main([], root=repo)
