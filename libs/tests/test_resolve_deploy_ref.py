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


def test_resolve_release_branch_targets_the_right_ref():
    runner = FakeRunner(
        stdout="2222222222222222222222222222222222222222\trefs/heads/release/1.2\n"
    )
    assert r.resolve_to_sha("release/1.2", runner=runner) == "2" * 40
    assert runner.calls[0][-1] == "refs/heads/release/1.2"


def test_resolve_annotated_tag_peels_to_underlying_commit():
    # An annotated tag: ls-remote lists the tag OBJECT sha and the peeled ^{} commit.
    # The resolver must return the commit, not the tag-object sha (finance_report
    # ships annotated tags, e.g. v0.1.7).
    runner = FakeRunner(
        stdout=(
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\trefs/tags/v1.2.3\n"
            "3333333333333333333333333333333333333333\trefs/tags/v1.2.3^{}\n"
        )
    )
    assert r.resolve_to_sha("v1.2.3", runner=runner) == "3" * 40


def test_resolve_lightweight_tag_uses_the_bare_ref():
    # A lightweight tag has no ^{} row; the bare ref already points at the commit.
    runner = FakeRunner(
        stdout="4444444444444444444444444444444444444444\trefs/tags/v1.2.3\n"
    )
    assert r.resolve_to_sha("v1.2.3", runner=runner) == "4" * 40


def test_resolve_missing_ref_raises():
    runner = FakeRunner(stdout="")  # ls-remote found nothing
    with pytest.raises(ValueError, match="not found"):
        r.resolve_to_sha("v9.9.9", runner=runner)


def test_resolve_wraps_git_failure_as_value_error():
    # A failing `git ls-remote` (non-zero exit) must surface as ValueError, not a raw
    # CalledProcessError, so the CLI gives a stable, actionable error.
    runner = FakeRunner(raises=subprocess.CalledProcessError(128, ["git", "ls-remote"]))
    with pytest.raises(ValueError, match="git ls-remote failed"):
        r.resolve_to_sha("main", runner=runner)


def test_resolve_wraps_missing_git_as_value_error():
    runner = FakeRunner(raises=FileNotFoundError("git not installed"))
    with pytest.raises(ValueError, match="git ls-remote failed"):
        r.resolve_to_sha("main", runner=runner)


def test_resolve_unknown_shape_raises_before_any_git_call():
    runner = FakeRunner(stdout="")
    with pytest.raises(ValueError, match="unrecognized"):
        r.resolve_to_sha("develop", runner=runner)
    assert runner.calls == []
