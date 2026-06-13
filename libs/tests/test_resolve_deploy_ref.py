"""Tests for the commit-addressed deploy surface resolver (finance_report#883, P2)."""

from __future__ import annotations

import subprocess

import pytest

from tools import resolve_deploy_ref as r


class FakeRunner:
    """Stand-in for subprocess.run that returns a canned ls-remote output."""

    def __init__(self, stdout: str = "", *, raises: Exception | None = None):
        self.stdout = stdout
        self.raises = raises
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(cmd, 0, stdout=self.stdout, stderr="")


@pytest.mark.parametrize(
    "ref, expected_kind",
    [
        ("main", "branch"),
        ("release/1.2", "release-branch"),
        ("release/10.4", "release-branch"),
        ("v1.2.3", "tag"),
        ("v10.0.1", "tag"),
        ("abc1234", "sha"),
        ("a" * 40, "sha"),
        ("  main  ", "branch"),
    ],
)
def test_classify_ref_recognizes_the_surface(ref, expected_kind):
    assert r.classify_ref(ref) == expected_kind


@pytest.mark.parametrize(
    "bad", ["", "   ", "develop", "release/1", "v1.2", "xyz", "1.2.3"]
)
def test_classify_ref_rejects_unknown_shapes(bad):
    with pytest.raises(ValueError):
        r.classify_ref(bad)


def test_resolve_sha_is_returned_verbatim_without_touching_git():
    runner = FakeRunner(stdout="should-not-be-used")
    assert r.resolve_to_sha("deadbeef", runner=runner) == "deadbeef"
    assert runner.calls == []  # a bare sha needs no remote lookup


def test_resolve_main_uses_refs_heads_main():
    runner = FakeRunner(
        stdout="1111111111111111111111111111111111111111\trefs/heads/main\n"
    )
    assert r.resolve_to_sha("main", runner=runner) == "1" * 40
    assert runner.calls[0][:2] == ["git", "ls-remote"]
    assert runner.calls[0][-1] == "refs/heads/main"


def test_resolve_release_branch_and_tag_target_the_right_ref():
    runner = FakeRunner(
        stdout="2222222222222222222222222222222222222222\trefs/heads/release/1.2\n"
    )
    assert r.resolve_to_sha("release/1.2", runner=runner) == "2" * 40
    assert runner.calls[0][-1] == "refs/heads/release/1.2"

    runner = FakeRunner(
        stdout="3333333333333333333333333333333333333333\trefs/tags/v1.2.3\n"
    )
    assert r.resolve_to_sha("v1.2.3", runner=runner) == "3" * 40
    assert runner.calls[0][-1] == "refs/tags/v1.2.3"


def test_resolve_missing_ref_raises():
    runner = FakeRunner(stdout="")  # ls-remote found nothing
    with pytest.raises(ValueError, match="not found"):
        r.resolve_to_sha("v9.9.9", runner=runner)


def test_resolve_unknown_shape_raises_before_any_git_call():
    runner = FakeRunner(stdout="")
    with pytest.raises(ValueError, match="unrecognized"):
        r.resolve_to_sha("develop", runner=runner)
    assert runner.calls == []
