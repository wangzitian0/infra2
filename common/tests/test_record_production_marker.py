"""Tests for tools.record_production_marker (#1511): every precondition, no network."""

from __future__ import annotations

import json
import subprocess

import pytest

from tools.record_production_marker import main

TAG = "v1.2.3"
MARKER = "production/v1.2.3"
SHA = "b" * 40
OTHER = "c" * 40
BUSY = ("waiting", "pending", "in_progress", "queued")
PROMOTION_TITLE = "reconcile-iac-inputs (PROD v1.2.3)"
LS_REMOTE = ["git", "ls-remote", "--tags", "origin", f"refs/tags/{MARKER}"]


def _row(conclusion: str, created: str, title: str = PROMOTION_TITLE) -> dict:
    return {"displayTitle": title, "conclusion": conclusion, "createdAt": created}


class FakeGit:
    """Fake runner. `answers` maps a command name to (returncode, stdout, stderr)."""

    def __init__(self, **override) -> None:
        self.calls: list[list[str]] = []
        self.pushed = False
        self.answers = {
            "resolve": (0, SHA + "\n", ""),
            "local_marker": (128, "", "missing"),
            "ancestor": (0, "", ""),
            "ls_remote": (0, "", ""),
            "ls_remote_after_push": (0, f"{SHA}\trefs/tags/{MARKER}\n", ""),
            "tag_list": (0, "", ""),
            "tag": (0, "", ""),
            "push": (0, "", ""),
            "dispatch": (
                0,
                json.dumps(
                    [
                        _row("failure", "2026-11-10T07:00:00Z"),
                        _row("success", "2026-11-10T08:00:00Z"),
                    ]
                ),
                "",
            ),
            **{f"busy:{status}": (0, "[]", "") for status in BUSY},
        }
        self.answers.update(override)

    def _name(self, argv: list[str]) -> str:
        if argv[:3] == ["gh", "run", "list"]:
            if "--status" in argv:
                return f"busy:{argv[argv.index('--status') + 1]}"
            return "dispatch"
        if argv[:3] == ["git", "ls-remote", "--tags"]:
            return "ls_remote_after_push" if self.pushed else "ls_remote"
        if argv[:3] == ["git", "tag", "--list"]:
            return "tag_list"
        if argv[:2] == ["git", "tag"]:
            return "tag"
        if argv[:2] == ["git", "push"]:
            return "push"
        if argv[:3] == ["git", "merge-base", "--is-ancestor"]:
            return "ancestor"
        if argv[:3] == ["git", "rev-parse", "--verify"]:
            return "resolve" if argv[3] == f"{TAG}^{{commit}}" else "local_marker"
        raise AssertionError(f"unexpected command: {argv}")

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))
        name = self._name(argv)
        code, out, err = self.answers[name]
        self.pushed = self.pushed or (name == "push" and code == 0)
        return subprocess.CompletedProcess(argv, code, out, err)

    @property
    def mutations(self) -> list[list[str]]:
        return [
            call
            for call in self.calls
            if call[:2] == ["git", "push"]
            or (call[:2] == ["git", "tag"] and call[2] != "--list")
        ]


def _run(capsys, fake: FakeGit, *args: str, tag: str = TAG) -> tuple[int, str]:
    rc = main([tag, *args], runner=fake)
    return rc, capsys.readouterr().out


def _refused(capsys, fake: FakeGit, line: str, **kwargs) -> None:
    rc, out = _run(capsys, fake, **kwargs)
    assert (rc, out) == (3, line + "\n")
    assert fake.mutations == []


@pytest.mark.parametrize("tag", ["main", "v1.2", "v1.2.3-rc.1", "1.2.3"])
def test_tool_refuses_a_tag_that_is_not_a_release_version(capsys, tag) -> None:
    fake = FakeGit()
    _refused(
        capsys,
        fake,
        f"::error::tag format: production promotion requires a version tag, got {tag!r}",
        tag=tag,
    )
    assert fake.calls == []


def test_tool_refuses_a_tag_that_does_not_resolve(capsys) -> None:
    fake = FakeGit(resolve=(128, "", "unknown revision"))
    _refused(
        capsys,
        fake,
        "::error::tag resolves: v1.2.3 does not resolve to a commit: unknown revision",
    )


def test_tool_refuses_a_tag_that_is_not_on_origin_main(capsys) -> None:
    fake = FakeGit(ancestor=(1, "", ""))
    _refused(
        capsys,
        fake,
        "::error::tag on main: bbbbbbbbbbbb is not an ancestor of origin/main",
    )
    assert fake.calls[-1] == ["git", "merge-base", "--is-ancestor", SHA, "origin/main"]


def test_tool_refuses_to_move_a_remote_marker_that_points_elsewhere(capsys) -> None:
    fake = FakeGit(ls_remote=(0, f"{OTHER}\trefs/tags/{MARKER}\n", ""))
    _refused(
        capsys,
        fake,
        "::error::remote marker: production/v1.2.3 points to cccccccccccc, "
        "not bbbbbbbbbbbb; a marker is immutable",
    )


def test_tool_reports_an_existing_marker_at_the_same_commit_as_recorded(capsys) -> None:
    fake = FakeGit(ls_remote=(0, f"{SHA}\trefs/tags/{MARKER}\n", ""))
    rc, out = _run(capsys, fake)

    assert (rc, out) == (0, f"already recorded {MARKER} at {SHA}\n")
    assert fake.mutations == []
    assert [call for call in fake.calls if call[0] == "gh"] == []


def test_tool_refuses_a_tag_older_than_the_latest_marker(capsys) -> None:
    fake = FakeGit(tag_list=(0, "production/v1.2.4\n", ""))
    _refused(
        capsys,
        fake,
        "::error::marker advances: production promotion cannot move backward "
        "from v1.2.4 to v1.2.3; publish a newer revert release instead",
    )


@pytest.mark.parametrize("status", BUSY)
def test_tool_refuses_while_a_reconcile_run_is_busy(capsys, status) -> None:
    fake = FakeGit(**{f"busy:{status}": (0, '[{"databaseId": 4242}]', "")})
    _refused(capsys, fake, f"::error::reconcile busy: {status}: 4242")
    assert [c for c in fake.calls if c[0] == "gh"][-1] == [
        "gh",
        "run",
        "list",
        "--workflow",
        "reconcile-iac-inputs.yml",
        "--status",
        "queued",
        "--json",
        "databaseId",
    ]


def test_tool_lists_every_busy_status_in_one_refusal(capsys) -> None:
    fake = FakeGit(
        **{
            "busy:waiting": (0, '[{"databaseId": 1}]', ""),
            "busy:in_progress": (0, '[{"databaseId": 2}, {"databaseId": 3}]', ""),
        }
    )
    _refused(capsys, fake, "::error::reconcile busy: waiting: 1; in_progress: 2, 3")


def test_tool_fails_closed_when_the_busy_listing_fails(capsys) -> None:
    fake = FakeGit(**{"busy:pending": (1, "", "boom")})
    _refused(
        capsys,
        fake,
        "::error::reconcile busy: gh run list --status pending failed: boom",
    )


def test_tool_refuses_when_no_promotion_run_has_the_tag_in_its_title(capsys) -> None:
    rows = [
        _row("success", "2026-11-10T08:00:00Z", "reconcile-iac-inputs (PROD v1.2.30)"),
        _row(
            "success", "2026-11-10T08:00:00Z", "reconcile-iac-inputs (STAGING v1.2.3)"
        ),
    ]
    fake = FakeGit(dispatch=(0, json.dumps(rows), ""))
    _refused(
        capsys,
        fake,
        "::error::promotion run: no workflow_dispatch run titled '(PROD v1.2.3)' "
        "in the newest 30",
    )


@pytest.mark.parametrize(
    ("rows", "ended"),
    [
        ([_row("failure", "2026-11-10T08:00:00Z")], "failure"),
        ([_row("", "2026-11-10T08:00:00Z")], ""),
        (
            [
                _row("success", "2026-11-10T07:00:00Z"),
                _row("cancelled", "2026-11-10T08:00:00Z"),
            ],
            "cancelled",
        ),
    ],
)
def test_tool_refuses_unless_the_newest_promotion_run_succeeded(
    capsys, rows, ended
) -> None:
    fake = FakeGit(dispatch=(0, json.dumps(rows), ""))
    _refused(
        capsys,
        fake,
        f"::error::promotion run: newest run titled '(PROD v1.2.3)' ended {ended!r}, "
        "not 'success'",
    )
    assert fake.calls[-1] == [
        "gh",
        "run",
        "list",
        "--workflow",
        "reconcile-iac-inputs.yml",
        "--event",
        "workflow_dispatch",
        "--limit",
        "30",
        "--json",
        "displayTitle,conclusion,createdAt",
    ]


def test_tool_reports_the_first_failing_precondition_only(capsys) -> None:
    fake = FakeGit(
        ancestor=(1, "", ""),
        **{"busy:waiting": (0, '[{"databaseId": 1}]', "")},
    )
    _refused(
        capsys,
        fake,
        "::error::tag on main: bbbbbbbbbbbb is not an ancestor of origin/main",
    )
    assert [call for call in fake.calls if call[0] == "gh"] == []


def test_tool_dry_run_prints_the_plan_and_changes_nothing(capsys) -> None:
    fake = FakeGit()
    rc, out = _run(capsys, fake, "--dry-run")

    assert (rc, out) == (0, f"would record {MARKER} at {SHA}\n")
    assert fake.mutations == []


def test_tool_tags_pushes_and_verifies_the_remote_marker(capsys) -> None:
    fake = FakeGit()
    rc, out = _run(capsys, fake)

    assert (rc, out) == (0, f"recorded {MARKER} at {SHA}\n")
    assert fake.mutations == [
        ["git", "tag", MARKER, SHA],
        ["git", "push", "origin", f"refs/tags/{MARKER}"],
    ]
    assert fake.calls[-1] == LS_REMOTE
    assert fake.calls.index(fake.mutations[1]) < len(fake.calls) - 1
    assert {call[0] for call in fake.calls} == {"git", "gh"}
    assert all(
        call[:3] == ["gh", "run", "list"] for call in fake.calls if call[0] == "gh"
    )


def test_tool_keeps_a_local_marker_that_already_points_to_the_commit(capsys) -> None:
    fake = FakeGit(local_marker=(0, SHA + "\n", ""))
    rc, out = _run(capsys, fake)

    assert (rc, out) == (0, f"recorded {MARKER} at {SHA}\n")
    assert fake.mutations == [["git", "push", "origin", f"refs/tags/{MARKER}"]]


def test_tool_never_moves_a_local_marker_that_points_elsewhere(capsys) -> None:
    fake = FakeGit(local_marker=(0, OTHER + "\n", ""))
    _refused(
        capsys,
        fake,
        "::error::local marker: production/v1.2.3 points to cccccccccccc, "
        "not bbbbbbbbbbbb",
    )


@pytest.mark.parametrize(
    ("step", "line"),
    [
        ("tag", "::error::tag create: fatal: bad object"),
        ("push", "::error::push: denied"),
    ],
)
def test_tool_exits_3_when_tagging_or_pushing_fails(capsys, step, line) -> None:
    detail = "fatal: bad object" if step == "tag" else "denied"
    fake = FakeGit(**{step: (1, "", detail)})
    rc, out = _run(capsys, fake)

    assert (rc, out) == (3, line + "\n")
    assert ("push" in {m[1] for m in fake.mutations}) is (step == "push")
    assert LS_REMOTE not in fake.calls[fake.calls.index(fake.mutations[-1]) :]


@pytest.mark.parametrize(
    ("after_push", "seen"),
    [("", ""), (f"{OTHER}\trefs/tags/{MARKER}\n", OTHER)],
)
def test_tool_exits_3_when_the_remote_does_not_show_the_marker_after_push(
    capsys, after_push, seen
) -> None:
    fake = FakeGit(ls_remote_after_push=(0, after_push, ""))
    rc, out = _run(capsys, fake)

    assert (rc, out) == (
        3,
        f"::error::verify: remote {MARKER} is {seen!r} after push, expected {SHA}\n",
    )
    assert fake.mutations[-1] == ["git", "push", "origin", f"refs/tags/{MARKER}"]
