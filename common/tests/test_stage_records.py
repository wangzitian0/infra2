"""Stage records: the writer, the CI post record, the reader and the reminder (#1645)."""

from __future__ import annotations

import io
import json
import urllib.error
import zipfile
from pathlib import Path

import pytest
import yaml
from infra2_sdk.stage_record import StageRecord

import common.deploy.reconcile_iac_inputs as reconcile
from common.deploy import stage_record as writer
from common.deploy import stage_record_reader as stage_records
from tools import promotion_reminder

ROOT = Path(__file__).resolve().parents[2]
RUN_URL = "https://github.com/o/r/actions/runs/777"
RECONCILE = ".github/workflows/reconcile-iac-inputs.yml"
TAG_COMMIT = "c" * 40


# -- the writer ---------------------------------------------------------------------------


def _drive_staging_reconcile(monkeypatch, tmp_path, *, returncode=0) -> Path:
    """Run `main` for a staging soak of v3.1.23 with fake deploys and one fake hook."""
    records = tmp_path / "records"
    monkeypatch.setenv("STAGE_RECORD_DIR", str(records))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_REF", "refs/tags/v3.1.23")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", "777")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(reconcile, "assert_after_on_main", lambda *_a, **_k: None)
    monkeypatch.setattr(
        reconcile,
        "run_deploy_commands",
        lambda cmds, **_k: [
            {
                "service": c.service,
                "type": c.deploy_type,
                "returncode": returncode,
                "stdout": "{}",
                "stderr": "boom" if returncode else "",
                "attempts": [{"n": 1, "cause": None, "at": "t"}],
                "seconds": 12.5,
            }
            for c in cmds
        ],
    )
    monkeypatch.setattr(
        reconcile,
        "run_post_deploy_app_hooks",
        lambda *_a, slot, **_k: [
            {
                "service": svc,
                "slot": slot,
                "status": "pass",
                "summary": "ok",
                "version_ref": "v3.1.23",
                "duration_seconds": 3.0,
            }
            for svc in ("platform/alerting", "platform/todo")
        ],
    )
    monkeypatch.setattr(
        reconcile,
        "run_canary_reality_gate",
        lambda **_k: (True, "canary_verify passed (staging, sdk=4.9.1)"),
    )
    rc = reconcile.main(
        [
            "--before",
            "v3.1.22",
            "--after",
            "v3.1.23",
            "--env",
            "staging",
            "--changed-file",
            "platform/12.alerting/compose.yaml",
        ]
    )
    assert rc == (1 if returncode else 0)
    return records


def test_writer_emits_three_valid_records(monkeypatch, tmp_path) -> None:
    records = _drive_staging_reconcile(monkeypatch, tmp_path)
    files = sorted(p.name for p in records.iterdir())
    assert files == [
        "stage-staging.release_validation-operation-tag-v3.1.23.json",
        "stage-staging.release_validation-post-tag-v3.1.23.json",
        "stage-staging.release_validation-pre-tag-v3.1.23.json",
    ]
    loaded = {
        p.name.split("-")[2]: StageRecord.from_dict(json.loads(p.read_text()))
        for p in records.iterdir()
    }
    assert set(loaded) == {"pre", "operation", "post"}
    assert {r.subject for r in loaded.values()} == {"tag:v3.1.23"}
    assert {r.event for r in loaded.values()} == {"tag"}
    assert {r.run_url for r in loaded.values()} == {RUN_URL}
    assert [c.name for c in loaded["pre"].checks] == [
        "contract",
        "provenance",
        "app_pins",
    ]
    # `hook_env` needs the secret values, which only the `operation` job holds (#1646)
    assert [(c.name, c.seconds) for c in loaded["operation"].checks][2:-1] == [
        ("staging:platform/alerting,platform/todo", 12.5),
        ("platform/alerting:smoke", 3.0),
        ("platform/todo:smoke", 3.0),
    ]
    assert loaded["operation"].checks[-1].name == "canary_verify"
    assert loaded["operation"].checks[-1].ok is True
    assert loaded["operation"].checks[0].name == "hook_env"
    # the runner SDK check runs before the first deploy unit (#1822)
    assert loaded["operation"].checks[1].name == "runner_sdk"
    assert [(c.name, c.seconds) for c in loaded["post"].checks] == [
        ("platform/alerting:staging_gate", 3.0),
        ("platform/todo:staging_gate", 3.0),
    ]
    # the gate hook records the version it checked: the production guard reads it back (#1563)
    assert loaded["post"].checks[0].detail == "[v3.1.23] ok"
    assert "infra:platform/alerting" in loaded["post"].units
    assert loaded["post"].uncovered == ()
    # sdk:X is the Canary image pin, not the runner's SDK (infra2-sdk#247)
    from common.deploy.canary_pin import canary_sdk_version

    assert f"sdk:{canary_sdk_version()}" in loaded["post"].units
    assert {r.verdict for r in loaded.values()} == {"pass"}


def test_failed_operation_writes_no_post_record_and_a_red_operation_record(
    monkeypatch, tmp_path
) -> None:
    records = _drive_staging_reconcile(monkeypatch, tmp_path, returncode=1)
    names = sorted(p.name.split("-")[2] for p in records.iterdir())
    assert names == ["operation", "pre"]
    operation = StageRecord.from_dict(
        json.loads(next(p for p in records.glob("*-operation-*")).read_text())
    )
    assert operation.verdict == "fail"
    deploy = next(c for c in operation.checks if c.name.startswith("staging:"))
    assert deploy.detail == "boom"


def test_a_failed_guard_writes_a_red_pre_record_and_reraises(
    monkeypatch, tmp_path
) -> None:
    def refuse(*_a, **_k):
        raise SystemExit("::error::tag is not on main")

    monkeypatch.setenv("STAGE_RECORD_DIR", str(tmp_path / "r"))
    monkeypatch.setattr(reconcile, "assert_after_on_main", refuse)
    with pytest.raises(SystemExit):
        reconcile.main(
            ["--after", "v3.1.23", "--env", "staging", "--changed-file", "x.md"]
        )
    (path,) = (tmp_path / "r").iterdir()
    record = StageRecord.from_dict(json.loads(path.read_text()))
    assert record.phase == "pre" and record.verdict == "fail"
    assert [(c.name, c.ok) for c in record.checks] == [
        ("contract", True),
        ("provenance", False),
    ]
    assert "not on main" in record.checks[1].detail


def test_a_dry_run_writes_no_record(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STAGE_RECORD_DIR", str(tmp_path / "r"))
    reconcile.main(
        [
            "--after",
            "v3.1.23",
            "--dry-run",
            "--changed-file",
            "platform/12.alerting/compose.yaml",
        ]
    )
    assert not (tmp_path / "r").exists()


def test_the_writer_names_each_upload_step_variable(monkeypatch, tmp_path) -> None:
    env_file = tmp_path / "github_env"
    monkeypatch.setenv("GITHUB_ENV", str(env_file))
    writer.write_stage_record(
        env="prod.release_integrity",
        phase="pre",
        subject="tag:v1.0.0",
        ref="refs/tags/v1.0.0",
        event="dispatch",
        run_url=RUN_URL,
        started_at="2026-11-01T00:00:00Z",
        checks=[writer.CheckVerdict("contract", True)],
        directory=tmp_path,
    )
    assert env_file.read_text() == (
        "STAGE_RECORD_PRE=stage-prod.release_integrity-pre-tag-v1.0.0\n"
    )
    assert (tmp_path / "stage-prod.release_integrity-pre-tag-v1.0.0.json").is_file()


def test_each_reconcile_upload_step_reads_the_variable_the_writer_sets() -> None:
    """Each train job uploads the record of its own phase (#1646)."""
    jobs = yaml.safe_load((ROOT / RECONCILE).read_text())["jobs"]
    for phase in ("pre", "operation", "post"):
        uploads = {
            s["name"]: s
            for s in jobs[phase]["steps"]
            if s.get("name", "").startswith("Upload stage record")
        }
        assert set(uploads) == {f"Upload stage record {phase}"}
        step = uploads[f"Upload stage record {phase}"]
        var = f"STAGE_RECORD_{phase.upper()}"
        assert step["if"] == f"always() && env.{var} != ''"
        assert step.get("continue-on-error") is not True
        assert step["with"]["name"] == f"${{{{ env.{var} }}}}"
        assert step["with"]["path"] == f"stage-records/${{{{ env.{var} }}}}.json"


# -- the CI post record of main -----------------------------------------------------------


def _blocking_infra_ci_jobs() -> set[str]:
    inventory = yaml.safe_load((ROOT / "docs/ssot/ci-gate-inventory.yaml").read_text())
    return {
        g["job"]
        for g in inventory["gates"]
        if g["workflow"] == ".github/workflows/infra-ci.yml" and g["blocks_merge"]
    }


def _ci_env(needs: dict) -> dict:
    return {
        "NEEDS_JSON": json.dumps({k: {"result": v} for k, v in needs.items()}),
        "GITHUB_SHA": "a" * 40,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "o/r",
        "GITHUB_RUN_ID": "777",
    }


def test_infra_ci_main_push_writes_one_ci_post_record(tmp_path) -> None:
    needs = {job: "success" for job in sorted(_blocking_infra_ci_jobs())}
    needs["lint-python"] = "failure"
    needs["validate-compose"] = "skipped"
    record = writer.ci_post_record(_ci_env(needs), tmp_path)
    (path,) = tmp_path.iterdir()
    assert path.name == f"stage-github_ci.merge_authority-post-sha-{'a' * 40}.json"
    loaded = StageRecord.from_dict(json.loads(path.read_text()))
    assert loaded == record
    assert (loaded.env, loaded.phase, loaded.event) == (
        "github_ci.merge_authority",
        "post",
        "push",
    )
    assert loaded.verdict == "fail"
    assert {c.name: c.ok for c in loaded.checks} == {
        **{job: True for job in needs},
        "lint-python": False,
    }


def test_a_cancelled_ci_run_writes_no_record(tmp_path) -> None:
    needs = {job: "success" for job in _blocking_infra_ci_jobs()}
    needs["lint-python"] = "cancelled"
    assert writer.ci_post_record(_ci_env(needs), tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_the_record_job_needs_every_blocking_job_and_runs_on_a_push_to_main_only() -> (
    None
):
    workflow = yaml.safe_load((ROOT / ".github/workflows/infra-ci.yml").read_text())
    job = workflow["jobs"]["record"]
    assert set(job["needs"]) == _blocking_infra_ci_jobs()
    assert job["if"] == (
        "always() && github.event_name == 'push' && github.ref == 'refs/heads/main'"
    )
    assert workflow[True]["push"]["branches"] == ["main"]
    upload = job["steps"][-1]
    assert upload["if"] == "always() && env.STAGE_RECORD_POST != ''"
    assert upload["with"]["name"] == "${{ env.STAGE_RECORD_POST }}"


# -- the reader ---------------------------------------------------------------------------


def _record(
    env="staging.release_validation", phase="post", subject="tag:v1.0.0", run=777
):
    return {
        "schema": 1,
        "env": env,
        "phase": phase,
        "subject": subject,
        "ref": "refs/tags/v1.0.0",
        "event": "tag",
        "run_url": f"https://github.com/o/r/actions/runs/{run}",
        "started_at": "2026-11-01T00:00:00Z",
        "finished_at": "2026-11-01T00:01:00Z",
        "checks": [{"name": "c", "ok": True, "detail": "", "seconds": 1.0}],
        "units": ["app:x"],
        "uncovered": [],
        "verdict": "pass",
    }


class FakeGitHub:
    """Serves artifacts, runs and zips. `add` takes the knobs that each provenance check reads."""

    def __init__(self) -> None:
        self.artifacts: list[dict] = []
        self.runs: dict[int, dict] = {}
        self.zips: dict[str, bytes] = {}
        self.auth_to_signed_host: list[str | None] = []
        #: the commit of every tag and the answer of ``compare/main...<sha>``
        self.commit = TAG_COMMIT
        self.compare_status = "behind"
        self.compare_urls: list[str] = []
        self.fail_on: str | None = None

    def add(
        self,
        record,
        *,
        run=777,
        path=RECONCILE,
        event="push",
        expired=False,
        repository_id=1,
        head_repository_id=1,
        name=None,
        created="2026-11-02T00:00:00Z",
        head_branch=None,
        head_sha=None,
    ):
        kind, _, ident = record["subject"].partition(":")
        # What a run of the right commit looks like: main for a commit, the tag for a tag push.
        branch = head_branch or (ident if kind == "tag" and event == "push" else "main")
        sha = head_sha or (self.commit if kind == "tag" else ident)
        name = name or stage_records.PREFIX + (
            f"{record['env']}-{record['phase']}-{record['subject'].replace(':', '-')}"
        )
        url = f"https://api.github.com/artifacts/{len(self.artifacts)}/zip"
        self.artifacts.append(
            {
                "name": name,
                "expired": expired,
                "created_at": created,
                "archive_download_url": url,
                "workflow_run": {
                    "id": run,
                    "repository_id": repository_id,
                    "head_repository_id": head_repository_id,
                },
            }
        )
        self.runs[run] = {
            "path": path,
            "event": event,
            "head_branch": branch,
            "head_sha": sha,
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(f"{name}.json", json.dumps(record))
        self.zips[url] = buffer.getvalue()

    def __call__(self, request, timeout=0):
        url = request.full_url
        if self.fail_on and self.fail_on in url:
            raise urllib.error.HTTPError(url, 502, "Bad Gateway", {}, None)
        if "/compare/" in url:
            assert "/compare/main..." in url, (
                f"expected base=main in compare url, got {url}"
            )
            self.compare_urls.append(url)
            body = {"status": self.compare_status}
        elif url.startswith("https://api.github.com/repos/o/r/actions/artifacts"):
            body = {
                "artifacts": sorted(
                    self.artifacts, key=lambda a: a["created_at"], reverse=True
                )
            }
        elif "/repos/o/r/commits/" in url:
            body = {"sha": self.commit}
        elif "/actions/runs/" in url:
            body = self.runs[int(url.rsplit("/", 1)[1])]
        elif url in self.zips:  # the API answers 302 to the signed host
            headers = {"Location": "https://signed.example/" + url.rsplit("/", 2)[1]}
            raise urllib.error.HTTPError(url, 302, "Found", headers, None)
        elif url.startswith("https://signed.example/"):
            self.auth_to_signed_host.append(request.get_header("Authorization"))
            index = url.rsplit("/", 1)[1]
            body = self.zips[f"https://api.github.com/artifacts/{index}/zip"]
            return _Raw(body)
        else:
            raise AssertionError(url)
        return _Raw(json.dumps(body).encode())


class _Raw:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self) -> bytes:
        return self.data


def _list(gh, **kw):
    return stage_records.list_records("o/r", "tok", opener=gh, **kw)


def test_list_returns_a_record_that_passes_every_provenance_check() -> None:
    gh = FakeGitHub()
    gh.add(_record())
    (found,) = _list(gh, env="staging")
    assert found["subject"] == "tag:v1.0.0"
    assert gh.auth_to_signed_host == [None]  # the signed host gets no token


@pytest.mark.parametrize(
    "knob",
    [
        {"expired": True},
        {"head_repository_id": 2},
        {"path": "not-the-writer"},
        {"event": "pull_request"},
        {"name": "stage-staging.release_validation-post-tag-v9.9.9"},
    ],
    ids=["expired", "fork", "workflow", "event", "name"],
)
def test_list_skips_an_artifact_that_fails_a_provenance_check(knob) -> None:
    gh = FakeGitHub()
    gh.add(_record(subject="tag:v1.0.0", run=1), run=1)  # the control: it must stay
    gh.add(_record(subject="tag:v2.0.0", run=2), run=2, **knob)
    assert [r["subject"] for r in _list(gh)] == ["tag:v1.0.0"]


@pytest.mark.parametrize(
    ("subject", "knob"),
    [
        # a branch push that uploads a green record for another SHA
        ("sha:" + "d" * 40, {"head_branch": "feature", "head_sha": "e" * 40}),
        # the right SHA on a branch that is not main
        ("sha:" + "d" * 40, {"head_branch": "feature"}),
        # main, but the run built another commit
        ("sha:" + "d" * 40, {"head_sha": "e" * 40}),
        # a tag push whose run built another commit than the tag
        ("tag:v2.0.0", {"head_sha": "e" * 40}),
        # a tag record from a branch push
        ("tag:v2.0.0", {"head_branch": "feature"}),
        # a dispatch from a branch other than main
        ("tag:v2.0.0", {"event": "workflow_dispatch", "head_branch": "feature"}),
    ],
    ids=[
        "branch-other-sha",
        "branch-right-sha",
        "main-other-sha",
        "tag-other-commit",
        "tag-branch",
        "dispatch-branch",
    ],
)
def test_list_refuses_a_record_that_its_run_did_not_build(subject, knob) -> None:
    gh = FakeGitHub()
    env = (
        "github_ci.merge_authority"
        if subject.startswith("sha")
        else "staging.release_validation"
    )
    path = ".github/workflows/infra-ci.yml" if subject.startswith("sha") else RECONCILE
    gh.add(
        _record(
            env=env,
            subject="sha:" + "a" * 40 if subject.startswith("sha") else "tag:v1.0.0",
            run=1,
        ),
        run=1,
        path=path,
    )
    gh.add(_record(env=env, subject=subject, run=2), run=2, path=path, **knob)
    kept = {r["subject"] for r in _list(gh)}
    assert subject not in kept and kept  # the control stays


def test_list_accepts_a_dispatched_tag_record_from_main() -> None:
    gh = FakeGitHub()
    gh.add(_record(subject="tag:v2.0.0"), event="workflow_dispatch")
    assert [r["subject"] for r in _list(gh)] == ["tag:v2.0.0"]


def test_list_skips_a_record_whose_run_url_names_another_run() -> None:
    gh = FakeGitHub()
    gh.add(_record(run=555), run=777)
    assert _list(gh) == []


def test_list_filters_by_stage_phase_subject_and_keeps_the_last_subjects() -> None:
    gh = FakeGitHub()
    for i, day in enumerate(("01", "02", "03")):
        for phase in ("pre", "operation", "post"):
            gh.add(
                _record(phase=phase, subject=f"tag:v1.0.{i}", run=10 + i),
                run=10 + i,
                created=f"2026-11-{day}T00:00:00Z",
            )
    gh.add(
        _record(env="prod.release_integrity", subject="tag:v0.9.0", run=99),
        run=99,
        created="2026-11-04T00:00:00Z",
    )
    assert {r["subject"] for r in _list(gh, env="staging", last=1)} == {"tag:v1.0.2"}
    assert len(_list(gh, env="staging", last=1)) == 3
    assert {r["env"] for r in _list(gh, env="prod")} == {"prod.release_integrity"}
    assert [
        r["phase"] for r in _list(gh, env="staging.release_validation", phase="post")
    ] == ["post"] * 3
    assert len(_list(gh, subject="tag:v1.0.0")) == 3


def _read(gh, **kw):
    kw = {
        "env": "staging.release_validation",
        "subject": "tag:v1.0.0",
        "event": "push",
    } | kw
    return stage_records.read_record("o/r", "tok", opener=gh, **kw)


def test_read_passes_a_green_record_of_a_tag_push_run_on_main() -> None:
    gh = FakeGitHub()
    gh.add(_record())
    reading = _read(gh)
    assert (reading.exit_code, reading.ok) == (0, True)
    assert "pass" in reading.detail and reading.record["subject"] == "tag:v1.0.0"


@pytest.mark.parametrize(
    ("knob", "reason"),
    [
        ({"expired": True}, "expired"),
        ({"head_repository_id": 2}, "fork"),
        ({"path": "not-the-writer"}, "workflow that owns the env"),
        ({"event": "pull_request"}, "event 'pull_request' is not allowed"),
        ({"event": "workflow_dispatch", "head_branch": "main"}, "not 'push'"),
        ({"head_sha": "e" * 40}, "did not build the commit"),
        ({"name": "stage-staging.release_validation-post-tag-v9.9.9"}, "no artifact"),
    ],
    ids=["expired", "fork", "workflow", "pull-request", "dispatch", "head-sha", "name"],
)
def test_reader_refuses_forged_or_stale(knob, reason) -> None:
    """AC11 (#1645, #1646): each of these records makes `read` exit 1, with the reason."""
    gh = FakeGitHub()
    gh.add(_record(), **knob)
    reading = _read(gh)
    assert reading.exit_code == 1, reading.detail
    assert reason in reading.detail


def test_reader_refuses_forged_or_stale_other_tag_and_forged_verdict() -> None:
    # a record for another tag
    gh = FakeGitHub()
    gh.add(_record(subject="tag:v2.0.0"))
    assert _read(gh).exit_code == 1
    # a verdict that says pass over a failed check: `read` judges the checks
    gh = FakeGitHub()
    forged = _record()
    forged["checks"] = [{"name": "truealpha/app:staging_gate", "ok": False}]
    assert forged["verdict"] == "pass"
    gh.add(forged)
    reading = _read(gh)
    assert reading.exit_code == 1 and "truealpha/app:staging_gate" in reading.detail
    # a record with no check at all
    gh = FakeGitHub()
    empty = _record()
    empty["checks"] = []
    gh.add(empty)
    assert _read(gh).exit_code == 1
    # a tag commit that is not on main
    gh = FakeGitHub()
    gh.compare_status = "diverged"
    gh.add(_record())
    reading = _read(gh)
    assert reading.exit_code == 1 and "not on main" in reading.detail


def test_reader_compares_against_main_base_so_older_main_commits_pass() -> None:
    """``compare/main...<sha>`` returns ``behind`` for older main commits (#1811)."""
    for status, expected_exit in (
        ("behind", 0),
        ("identical", 0),
        ("ahead", 1),
        ("diverged", 1),
    ):
        gh = FakeGitHub()
        gh.compare_status = status
        gh.add(_record())
        reading = _read(gh)
        assert reading.exit_code == expected_exit, status
        assert gh.compare_urls == [
            f"https://api.github.com/repos/o/r/compare/main...{TAG_COMMIT}"
        ]
        if expected_exit == 1:
            assert "not on main" in reading.detail


def test_reader_refuses_forged_or_stale_api_failure_is_exit_four() -> None:
    for broken in ("/actions/artifacts", "/actions/runs/", "/compare/"):
        gh = FakeGitHub()
        gh.add(_record())
        gh.fail_on = broken
        reading = _read(gh)
        assert reading.exit_code == 4, broken
        assert "cannot read" in reading.detail


def test_read_skips_a_newer_dispatch_run_and_judges_the_push_run_below_it() -> None:
    gh = FakeGitHub()
    gh.add(_record(run=1), run=1, created="2026-11-01T00:00:00Z")
    gh.add(
        _record(run=2),
        run=2,
        event="workflow_dispatch",
        head_branch="main",
        created="2026-11-02T00:00:00Z",
    )
    reading = _read(gh)
    assert reading.exit_code == 0 and reading.record["run_url"].endswith("/runs/1")


def test_read_judges_the_newest_record_of_a_rerun() -> None:
    red = _record(run=1)
    red["checks"] = [{"name": "c", "ok": False, "detail": "", "seconds": 1.0}]
    gh = FakeGitHub()
    gh.add(red, run=1, created="2026-11-01T00:00:00Z")
    gh.add(_record(run=2), run=2, created="2026-11-02T00:00:00Z")
    assert _read(gh).exit_code == 0


def test_the_read_command_maps_the_verdict_to_the_exit_code(
    monkeypatch, capsys
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    argv = ["read", "--repo", "o/r", "--env", "staging.release_validation",
            "--subject", "tag:v1.0.0", "--event", "push"]  # fmt: skip
    for code in (0, 1, 4):
        monkeypatch.setattr(
            stage_records,
            "read_record",
            lambda *_a, _code=code, **_k: stage_records.Reading(
                _code, f"detail {_code}"
            ),
        )
        assert stage_records.main(argv) == code
        assert f"detail {code}" in capsys.readouterr().out
    monkeypatch.delenv("GITHUB_TOKEN")
    assert stage_records.main(argv) == 2


@pytest.mark.parametrize(
    ("checks", "expected"),
    [
        ([], "missing staging_gate for finance_report@v1.2.9"),
        (
            [("finance_report:smoke", "[v1.2.9] ok", True)],
            "missing staging_gate for finance_report@v1.2.9",
        ),
        (
            [("finance_report:staging_gate", "[v1.2.8] ok", True)],
            "missing staging_gate for finance_report@v1.2.9",
        ),
        (
            [("finance_report:staging_gate", "[v1.2.9] boom", False)],
            "failed staging_gate for finance_report@v1.2.9",
        ),
        (
            [
                ("finance_report:staging_gate", "[v1.2.9] ok", True),
                ("finance_report:staging_gate", "[v1.2.9] boom", False),
            ],
            "failed staging_gate for finance_report@v1.2.9",
        ),
    ],
    ids=["empty", "only-smoke", "wrong-version", "failed", "mixed"],
)
def test_the_staging_gate_verdict_refuses_a_missing_or_failed_release(
    checks, expected
) -> None:
    """The cases of the old `judge_staging_gate` (#1493, #1563), on post records."""
    record = {"checks": [{"name": n, "detail": d, "ok": ok} for n, d, ok in checks]}
    ok, detail = stage_records.staging_gates_verdict(
        [record], {"finance_report": "v1.2.9"}
    )
    assert ok is False and expected in detail


def test_the_staging_gate_verdict_passes_a_green_release_and_an_empty_requirement() -> (
    None
):
    record = {
        "checks": [
            {"name": "finance_report:smoke", "detail": "[v1.2.9] ok", "ok": True},
            {
                "name": "finance_report:staging_gate",
                "detail": "[v1.2.9] ok",
                "ok": True,
            },
        ]
    }
    ok, detail = stage_records.staging_gates_verdict(
        [record], {"finance_report": "v1.2.9"}
    )
    assert ok is True and "staging_gate passed for finance_report@v1.2.9" in detail
    assert stage_records.staging_gates_verdict([], {})[0] is True


def test_the_tools_entry_is_the_reader() -> None:
    import tools.stage_records as shim

    assert shim.main is stage_records.main


def test_stage_records_reader_has_no_third_party_import() -> None:
    """Only the standard library and the one GET helper of ``common.deploy``."""
    source = (ROOT / "common/deploy/stage_record_reader.py").read_text()
    imports = {
        line.split()[1].split(".")[0]
        for line in source.splitlines()
        if line.startswith(("import ", "from "))
    }
    stdlib = set(__import__("sys").stdlib_module_names)
    assert imports <= stdlib | {"common"}
    with pytest.raises(SystemExit):  # `read` needs an env and a subject
        stage_records.main(["read"])
    assert "\t" not in source.split("def main")[1]


# -- the daily reminder -------------------------------------------------------------------


def _post(tag, verdict="pass"):
    return {**_record(subject=f"tag:{tag}"), "verdict": verdict}


def test_reminder_names_the_newest_green_staging_tag_that_is_not_in_production() -> (
    None
):
    staging = [_post("v1.0.1"), _post("v1.0.2"), _post("v1.0.3", "fail")]
    record = promotion_reminder.pending_promotion(staging, [], [])
    assert record["subject"] == "tag:v1.0.2"


@pytest.mark.parametrize(
    ("production", "markers"),
    [
        ([_post("v1.0.2")], []),
        ([], ["production/v1.0.2"]),
        ([], ["production/v1.0.9"]),
    ],
    ids=["prod-record", "marker", "newer-marker"],
)
def test_reminder_is_silent_for_a_promoted_tag(production, markers) -> None:
    assert (
        promotion_reminder.pending_promotion([_post("v1.0.2")], production, markers)
        is None
    )


def test_reminder_still_names_a_tag_whose_production_run_failed() -> None:
    record = promotion_reminder.pending_promotion(
        [_post("v1.0.2")], [_post("v1.0.2", "fail")], []
    )
    assert record["subject"] == "tag:v1.0.2"


def test_the_card_holds_the_tag_the_record_and_the_command_and_dispatches_nothing() -> (
    None
):
    card = promotion_reminder.reminder_card(_post("v1.0.2"))
    blob = json.dumps(card)
    assert (
        "v1.0.2 is green in staging and not in production"
        in card["header"]["title"]["content"]
    )
    assert "after=v1.0.2" in blob and RUN_URL in blob
    source = (ROOT / "tools/promotion_reminder.py").read_text()
    assert "subprocess" not in source and "workflow_dispatch" not in source


def test_the_reminder_job_runs_at_2100_utc_plus_8_and_reads_only() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ops-checks.yml").read_text())
    assert {"cron": "0 13 * * *"} in workflow["on"]["schedule"]
    digest = workflow["jobs"]["digest"]
    step = next(s for s in digest["steps"] if s["name"].startswith("Remind the owner"))
    assert step["run"] == "python tools/promotion_reminder.py"
    assert "0 13 * * *" in step["if"]
    assert (
        digest["permissions"]["contents"] == digest["permissions"]["actions"] == "read"
    )


# -- the post record follows the production marker; a record never turns a deploy red ---------


def _drive_prod_promotion(monkeypatch, tmp_path, marker):
    records = tmp_path / "records"
    monkeypatch.setenv("STAGE_RECORD_DIR", str(records))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(reconcile, "assert_after_on_main", lambda *_a, **_k: None)
    monkeypatch.setattr(reconcile, "require_upstream_post", lambda *_a, **_k: None)
    monkeypatch.setattr(
        reconcile, "assert_production_marker_advances", lambda *_a, **_k: "v3.1.22"
    )
    monkeypatch.setattr(reconcile, "record_production_marker", marker)
    monkeypatch.setattr(
        reconcile,
        "run_deploy_commands",
        lambda cmds, **_k: [
            {
                "service": c.service,
                "type": c.deploy_type,
                "returncode": 0,
                "stdout": "{}",
            }
            for c in cmds
        ],
    )
    monkeypatch.setattr(reconcile, "run_post_deploy_app_hooks", lambda *_a, **_k: [])
    monkeypatch.setattr(
        reconcile, "run_canary_reality_gate", lambda *_a, **_k: (True, "ok")
    )
    argv = [
        "--before", "v3.1.22", "--after", "v3.1.23", "--promote-prod",
        "--changed-file", "platform/12.alerting/compose.yaml",
    ]  # fmt: skip
    return records, argv


def _operation_of(records: Path) -> StageRecord:
    return StageRecord.from_dict(
        json.loads(next(records.glob("*-operation-*")).read_text())
    )


def test_the_operation_record_follows_a_recorded_production_marker(
    monkeypatch, tmp_path
) -> None:
    order: list[str] = []
    records, argv = _drive_prod_promotion(
        monkeypatch,
        tmp_path,
        lambda tag, *_a, **_k: order.append("marker") or f"production/{tag}",
    )
    real_emit = reconcile.StageRun.emit
    monkeypatch.setattr(
        reconcile.StageRun,
        "emit",
        lambda self, phase, *a, **k: (
            order.append(phase) or real_emit(self, phase, *a, **k)
        ),
    )
    assert reconcile.main(argv) == 0
    # the marker is written by the operation, before its record; operation then writes the post record
    assert order == ["pre", "marker", "operation", "post"]
    operation = _operation_of(records)
    assert operation.verdict == "pass"
    assert [(c.name, c.ok, c.detail) for c in operation.checks][-1] == (
        "production_marker", True, "production/v3.1.23",
    )  # fmt: skip


def test_a_rejected_production_marker_gives_a_failed_operation_record(
    monkeypatch, tmp_path
) -> None:
    def rejected(tag, *_a, **_k):
        raise reconcile.ProductionMarkerRejected("GitHub App push refused")

    records, argv = _drive_prod_promotion(monkeypatch, tmp_path, rejected)
    assert reconcile.main(argv) == 0  # the deploy outcome does not change
    operation = _operation_of(records)
    assert operation.verdict == "fail"
    marker = operation.checks[-1]
    assert (marker.name, marker.ok) == ("production_marker", False)
    assert "GitHub App push refused" in marker.detail


def test_a_crashing_production_marker_writes_the_failed_operation_record_then_raises(
    monkeypatch, tmp_path
) -> None:
    def crash(tag, *_a, **_k):
        raise SystemExit("cannot push production marker")

    records, argv = _drive_prod_promotion(monkeypatch, tmp_path, crash)
    with pytest.raises(SystemExit, match="cannot push production marker"):
        reconcile.main(argv)
    operation = _operation_of(records)
    assert operation.verdict == "fail"
    assert operation.checks[-1].name == "production_marker"
    assert "cannot push production marker" in operation.checks[-1].detail


def test_the_sdk_unit_never_raises(monkeypatch, capsys) -> None:
    def missing(_name):
        raise writer.PackageNotFoundError

    monkeypatch.setattr(writer, "version", missing)
    assert writer.sdk_unit() == "sdk:unknown"
    assert "::warning::" in capsys.readouterr().out


def test_reconcile_record_upload_steps_fail_closed() -> None:
    for job in ("pre", "operation", "post"):
        steps = yaml.safe_load((ROOT / RECONCILE).read_text())["jobs"][job]["steps"]
        uploads = [s for s in steps if "STAGE_RECORD_" in str(s.get("if", ""))]
        assert len(uploads) == 1, (RECONCILE, job)
        assert all(s.get("continue-on-error") is not True for s in uploads), (
            RECONCILE,
            job,
        )

    ci_steps = yaml.safe_load((ROOT / ".github/workflows/infra-ci.yml").read_text())[
        "jobs"
    ]["record"]["steps"]
    ci_uploads = [s for s in ci_steps if "STAGE_RECORD_" in str(s.get("if", ""))]
    assert len(ci_uploads) == 1
    assert all(s.get("continue-on-error") is True for s in ci_uploads)


def test_one_github_get_helper_and_one_card_publish_flow() -> None:
    import tools.ci_tree_reuse as reuse
    from common.alerting import release_card, stage_card
    from common.deploy import github_api

    assert reuse.get_json is github_api.get_json
    assert stage_records.get_json is github_api.get_json
    assert stage_card.publish_card is release_card.publish_card
    for name in ("_headers",):
        assert not hasattr(stage_records, name)
    assert not hasattr(release_card, "_deliver_card")


def test_verified_sdk_versions_returns_only_canary_staging_gate_passed_versions() -> (
    None
):
    """#1635: only staging.release_validation post records whose Canary staging_gate is ok verify sdk:X."""
    from common.deploy.stage_record_reader import verified_sdk_versions

    records = [
        # Valid Canary pass (even when downstream finance_report failed)
        {
            "env": "staging.release_validation",
            "phase": "post",
            "event": "tag",
            "subject": "tag:v3.1.73",
            "verdict": "fail",
            "units": ["platform/todo", "finance_report/app", "sdk:3.29.0"],
            "checks": [
                {"name": "platform/todo:staging_gate", "ok": True},
                {"name": "finance_report/app:staging_gate", "ok": False},
            ],
        },
        # Canary staging_gate failed -> must NOT verify 3.30.0
        {
            "env": "staging.release_validation",
            "phase": "post",
            "event": "tag",
            "subject": "tag:v3.1.74",
            "verdict": "fail",
            "units": ["platform/todo", "sdk:3.30.0"],
            "checks": [
                {"name": "platform/todo:staging_gate", "ok": False},
            ],
        },
        # Non-tag event (e.g. schedule) -> must NOT verify 3.31.0
        {
            "env": "staging.release_validation",
            "phase": "post",
            "event": "schedule",
            "subject": "tag:v3.1.75",
            "units": ["sdk:3.31.0"],
            "checks": [{"name": "platform/todo:staging_gate", "ok": True}],
        },
        # Wrong env / phase -> ignored
        {
            "env": "preview.runtime",
            "phase": "post",
            "event": "pull_request",
            "subject": "tag:v3.1.76",
            "units": ["sdk:3.32.0"],
            "checks": [{"name": "platform/todo:staging_gate", "ok": True}],
        },
        # Unrecognized placeholder -> ignored
        {
            "env": "staging.release_validation",
            "phase": "post",
            "event": "tag",
            "subject": "tag:v3.1.77",
            "units": ["sdk:unknown"],
            "checks": [{"name": "staging_gate:platform/todo", "ok": True}],
        },
    ]
    assert verified_sdk_versions(records) == {"3.29.0"}


def _sdk_record(
    version: str, tag: str, *, run: int = 777, canary_ok: bool = True
) -> dict:
    record = _record(subject=f"tag:{tag}", run=run)
    record["units"] = ["platform/todo", f"sdk:{version}"]
    record["checks"] = [{"name": "platform/todo:staging_gate", "ok": canary_ok}]
    return record


class _CountingGitHub(FakeGitHub):
    """A FakeGitHub that counts the run reads: one run read for each record it checks."""

    def __init__(self) -> None:
        super().__init__()
        self.run_reads = 0

    def __call__(self, request, timeout=0):
        if "/actions/runs/" in request.full_url:
            self.run_reads += 1
        return super().__call__(request, timeout)


def _sdk_window() -> _CountingGitHub:
    """Newest first: 4.4.0 verified, a red Canary of 4.5.0, a fork run of 4.6.0, then 4.3.0."""
    gh = _CountingGitHub()
    gh.add(
        _sdk_record("4.4.0", "v1.0.4", run=104), run=104, created="2026-11-04T00:00:00Z"
    )
    gh.add(
        _sdk_record("4.5.0", "v1.0.3", run=103, canary_ok=False),
        run=103,
        created="2026-11-03T00:00:00Z",
    )
    gh.add(
        _sdk_record("4.6.0", "v1.0.2", run=102),
        run=102,
        created="2026-11-02T00:00:00Z",
        head_repository_id=2,
    )
    gh.add(
        _sdk_record("4.3.0", "v1.0.1", run=101), run=101, created="2026-11-01T00:00:00Z"
    )
    return gh


def _verified(gh, wanted=None) -> set[str]:
    return stage_records.verified_sdk_versions(
        repo="o/r", token="tok", since_days=0, opener=gh, wanted=wanted
    )


def test_verified_sdk_scan_stops_when_each_wanted_version_is_verified() -> None:
    full = _sdk_window()
    assert _verified(full) == {"4.4.0", "4.3.0"}
    assert full.run_reads == 3  # the fork artifact fails before its run is read
    early = _sdk_window()
    assert _verified(early, wanted={"4.4.0"}) == {"4.4.0"}
    assert early.run_reads == 1


def test_verified_sdk_scan_keeps_the_verdict_of_the_full_scan() -> None:
    """A wanted version is in the result only when the full scan verifies it. A red Canary
    record and a fork record never verify their version, and the scan goes past them."""
    full = _verified(_sdk_window())
    for wanted in (
        {"4.4.0"},
        {"4.3.0"},
        {"4.4.0", "4.3.0"},
        {"4.5.0"},
        {"4.6.0"},
        {"9.9.9"},
        {"4.4.0", "9.9.9"},
    ):
        gh = _sdk_window()
        assert _verified(gh, wanted=wanted) & wanted == full & wanted, wanted
    for unverified in ({"4.5.0"}, {"4.6.0"}, {"9.9.9"}):
        gh = _sdk_window()
        assert _verified(gh, wanted=unverified) == full
        assert gh.run_reads == 3, unverified  # the full window, as without `wanted`
    empty = _CountingGitHub()
    assert _verified(empty, wanted={"4.4.0"}) == set()
    assert _verified(empty) == set()


def test_verified_sdk_scan_with_records_ignores_wanted() -> None:
    records = [_sdk_record("4.4.0", "v1.0.4"), _sdk_record("4.3.0", "v1.0.1")]
    assert stage_records.verified_sdk_versions(records, wanted={"4.4.0"}) == {
        "4.4.0",
        "4.3.0",
    }


def test_verified_sdk_versions_revokes_on_newer_failure_and_restores_on_newer_pass() -> (
    None
):
    """Latest-wins revocation: newer Canary failure revokes older pass; newer pass restores."""
    from common.deploy.stage_record_reader import verified_sdk_versions

    revoked_records = [
        _sdk_record("4.9.1", "v3.2.15", run=105, canary_ok=False),
        _sdk_record("4.9.1", "v3.2.14", run=104, canary_ok=True),
    ]
    assert verified_sdk_versions(revoked_records) == set()

    # Failure in canary_verify also revokes even if staging_gate is green.
    cv_fail = _sdk_record("4.9.1", "v3.2.15", run=105, canary_ok=True)
    cv_fail["checks"].append({"name": "platform/todo:canary_verify", "ok": False})
    assert (
        verified_sdk_versions([cv_fail, _sdk_record("4.9.1", "v3.2.14", run=104)])
        == set()
    )

    # Recovery: newest pass supersedes older failure.
    recovered = [
        _sdk_record("4.9.1", "v3.2.16", run=106, canary_ok=True),
        _sdk_record("4.9.1", "v3.2.15", run=105, canary_ok=False),
    ]
    assert verified_sdk_versions(recovered) == {"4.9.1"}


# -- a GitHub GET that fails with 5xx is sent again (#1645) -------------------------------------


class _Flaky:
    """Raise each error of ``errors`` in turn, then answer ``body``."""

    def __init__(self, errors, body=b'{"ok": true}') -> None:
        self.errors = list(errors)
        self.body = body
        self.calls = 0

    def __call__(self, request, timeout=0):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return _Raw(self.body)


def _http(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("u", code, "x", {}, None)


def test_get_json_repeats_a_5xx_or_network_error_and_returns_the_answer(
    monkeypatch,
) -> None:
    from common.deploy import github_api

    waits: list[float] = []
    monkeypatch.setattr(github_api, "_sleep", waits.append)
    flaky = _Flaky([_http(502), urllib.error.URLError("reset"), _http(503)])
    assert github_api.get_json("/x", "tok", flaky, "a") == {"ok": True}
    assert flaky.calls == 4
    assert waits == [1.0, 2.0, 4.0]


def test_get_json_raises_the_last_5xx_after_the_bounded_attempts() -> None:
    from common.deploy import github_api

    flaky = _Flaky([_http(502)] * 10)
    with pytest.raises(urllib.error.HTTPError) as raised:
        github_api.get_json("/x", "tok", flaky, "a")
    assert raised.value.code == 502
    assert flaky.calls == len(github_api.RETRY_DELAYS) + 1 == 4


@pytest.mark.parametrize("code", [302, 401, 404, 422])
def test_get_json_does_not_repeat_a_non_5xx_error(code) -> None:
    from common.deploy import github_api

    flaky = _Flaky([_http(code)])
    with pytest.raises(urllib.error.HTTPError):
        github_api.get_json("/x", "tok", flaky, "a")
    assert flaky.calls == 1


def test_list_reads_a_record_when_the_api_and_the_signed_host_fail_once() -> None:
    """The v3.1.154 failure: one HTTP 502 of the GitHub API. A repeat reads the record."""

    class _FailOnce(FakeGitHub):
        def __init__(self) -> None:
            super().__init__()
            self.failed: list[str] = []

        def __call__(self, request, timeout=0):
            url = request.full_url
            for part in ("/actions/artifacts", "/actions/runs/", "signed.example"):
                if part in url and part not in self.failed:
                    self.failed.append(part)
                    raise _http(502)
            return super().__call__(request, timeout)

    gh = _FailOnce()
    gh.add(_record())
    (found,) = _list(gh, env="staging")
    assert found["subject"] == "tag:v1.0.0"
    assert gh.failed == ["/actions/artifacts", "/actions/runs/", "signed.example"]


# -- a production run writes its post record in `operation` (#1646) -----------------------------


def _prod_run_with_gate(monkeypatch, tmp_path, *, gate_ok: bool):
    """Drive a production promotion whose hook runner returns smoke and prod_gate results."""
    records, argv = _drive_prod_promotion(
        monkeypatch, tmp_path, lambda tag, *_a, **_k: f"production/{tag}"
    )

    def hooks(plan, applied, *, slot, **_kw):
        return [
            {
                "service": "platform/alerting",
                "slot": one,
                "status": "fail" if one == "prod_gate" and not gate_ok else "pass",
                "summary": "boom" if one == "prod_gate" and not gate_ok else "ok",
                "version_ref": "v3.1.23",
                "duration_seconds": 1.0,
            }
            for one in slot.split(",")
        ]

    monkeypatch.setattr(reconcile, "run_post_deploy_app_hooks", hooks)
    code = reconcile.main(argv)
    loaded = [
        StageRecord.from_dict(json.loads(f.read_text())) for f in records.iterdir()
    ]
    return code, loaded


def test_a_passing_production_run_writes_a_post_record_and_the_card_says_passed(
    monkeypatch, tmp_path
) -> None:
    from common.alerting.stage_card import render_stage_card

    code, loaded = _prod_run_with_gate(monkeypatch, tmp_path, gate_ok=True)
    assert code == 0
    post = [r for r in loaded if r.phase == "post"]
    assert len(post) == 1 and post[0].verdict == "pass"
    assert [c.name for c in post[0].checks] == ["platform/alerting:prod_gate"]
    assert "Passed" in render_stage_card(loaded)["header"]["title"]["content"]


def test_a_red_prod_gate_writes_a_red_post_record_and_no_marker(
    monkeypatch, tmp_path
) -> None:
    from common.alerting.stage_card import render_stage_card

    code, loaded = _prod_run_with_gate(monkeypatch, tmp_path, gate_ok=False)
    assert code == 1
    post = [r for r in loaded if r.phase == "post"]
    assert len(post) == 1 and post[0].verdict == "fail"
    assert "Failed" in render_stage_card(loaded)["header"]["title"]["content"]
    operation = next(r for r in loaded if r.phase == "operation")
    assert "production_marker" not in [c.name for c in operation.checks]


def test_the_digest_counts_a_passing_production_run_and_the_reminder_stays_silent(
    monkeypatch, tmp_path
) -> None:
    from common.observability.delivery_section import summarize_delivery

    _, loaded = _prod_run_with_gate(monkeypatch, tmp_path, gate_ok=True)
    dicts = [r.to_dict() for r in loaded]
    assert {d["phase"] for d in dicts} == {"pre", "operation", "post"}
    assert summarize_delivery(dicts)["prod"]["all"]["passed"] == 1
    # the production post record marks the tag promoted, with or without a marker tag
    post = [d for d in dicts if d["phase"] == "post"]
    assert promotion_reminder.pending_promotion([_post("v3.1.23")], post, []) is None


def test_hookless_production_promotion_writes_post_record_and_card_passes(
    monkeypatch, tmp_path
) -> None:
    """A hookless production promotion still writes a passing `post` record so stage_card ends Passed."""
    from common.alerting.stage_card import render_stage_card

    records, argv = _drive_prod_promotion(
        monkeypatch, tmp_path, lambda tag, *_a, **_k: f"production/{tag}"
    )
    monkeypatch.setattr(reconcile, "run_post_deploy_app_hooks", lambda *_a, **_k: [])
    assert reconcile.main(argv) == 0
    loaded = [
        StageRecord.from_dict(json.loads(f.read_text())) for f in records.iterdir()
    ]
    post = [r for r in loaded if r.phase == "post"]
    assert len(post) == 1 and post[0].verdict == "pass"
    assert post[0].checks[0].name == "hooks" and post[0].checks[0].ok is True
    assert render_stage_card(loaded)["header"]["title"]["content"].endswith("Passed")


def test_staging_provider_regression_writer_and_scheduled_audit_stage_record(
    monkeypatch, tmp_path
) -> None:
    """scheduled_audit emits a valid staging.provider_regression StageRecord and WRITERS trusts ops-checks.yml."""
    assert stage_records.WRITERS["staging.provider_regression"] == (
        ".github/workflows/ops-checks.yml",
        frozenset({"schedule", "workflow_dispatch"}),
    )

    empty_checks = reconcile._scheduled_audit_stage_checks([])
    assert len(empty_checks) == 1
    assert empty_checks[0].ok is False

    expanded = reconcile._scheduled_audit_stage_checks(
        [
            {
                "service": "finance_report/app",
                "slot": "scheduled_audit",
                "status": "fail",
                "checks": [
                    {
                        "name": "audit_replay",
                        "ok": False,
                        "seconds": 12.0,
                        "detail": "failed",
                    },
                    {
                        "name": "audit_replay",
                        "ok": True,
                        "seconds": 1.0,
                        "skipped": True,
                        "detail": "skipped",
                    },
                ],
            }
        ]
    )
    assert [c.name for c in expanded] == [
        "finance_report/app:audit_replay",
        "finance_report/app:audit_replay#1",
    ]
    assert expanded[0].ok is False
    assert expanded[1].skipped is True and expanded[1].ok is False

    record_dir = tmp_path / "stage-records"
    github_env = tmp_path / "github_env"
    monkeypatch.setenv("STAGE_RECORD_DIR", str(record_dir))
    monkeypatch.setenv("GITHUB_ENV", str(github_env))
    monkeypatch.setenv("GITHUB_SHA", TAG_COMMIT)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "wangzitian0/infra2")
    monkeypatch.setenv("GITHUB_RUN_ID", "888")
    monkeypatch.setattr(reconcile, "sdk_unit", lambda: "sdk:4.6.0")

    record = reconcile._emit_scheduled_audit_stage_record(
        [
            {
                "service": "finance_report/app",
                "slot": "scheduled_audit",
                "status": "pass",
                "checks": [
                    {
                        "name": "pdf_corpus_oracle",
                        "ok": True,
                        "seconds": 4.2,
                        "detail": "ok",
                    }
                ],
            }
        ],
        repo_root=tmp_path,
        started_at="2026-11-25T02:37:00Z",
    )
    assert record is not None
    expected_name = f"stage-staging.provider_regression-post-sha-{TAG_COMMIT}"
    record_file = record_dir / f"{expected_name}.json"
    assert record_file.is_file()
    loaded = StageRecord.from_dict(json.loads(record_file.read_text(encoding="utf-8")))
    assert loaded.env == "staging.provider_regression"
    assert loaded.phase == "post"
    assert loaded.event == "schedule"
    assert loaded.subject == f"sha:{TAG_COMMIT}"
    assert loaded.verdict == "pass"
    assert [c.name for c in loaded.checks] == ["finance_report/app:pdf_corpus_oracle"]
    assert f"STAGE_RECORD_POST={expected_name}" in github_env.read_text(
        encoding="utf-8"
    )
