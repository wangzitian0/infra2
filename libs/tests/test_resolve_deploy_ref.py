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
        ("v1.2.3", "tag"),
        ("v10.0.1", "tag"),
        ("abc1234", "sha"),
        ("a" * 40, "sha"),
        ("DEADBEEF", "sha"),  # uppercase hex (copy/paste) is accepted
        ("AbC1234", "sha"),
        ("  main  ", "branch"),
    ],
)
def test_classify_ref_recognizes_the_surface(ref, expected_kind):
    assert r.classify_ref(ref) == expected_kind


@pytest.mark.parametrize(
    "bad",
    # release/x.y is retired: staging/prod deploy tags only, so it is no longer a
    # recognized surface form.
    ["", "   ", "develop", "release/1", "release/1.2", "v1.2", "xyz", "1.2.3"],
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


class _RealisticLsRemote:
    """Models `git ls-remote` faithfully: the peeled `^{}` row for an annotated tag is
    only emitted when that peeled ref is in the query args (an exact-ref query does NOT
    return it). This is the behaviour the old FakeRunner hid, letting the v1.1.6
    tag-object-sha bug ship green."""

    def __init__(self, *, tag: str, tag_obj_sha: str, commit_sha: str):
        self.tag_ref = f"refs/tags/{tag}"
        self.peeled_ref = self.tag_ref + "^{}"
        self.tag_obj_sha = tag_obj_sha
        self.commit_sha = commit_sha
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **_kwargs):
        self.calls.append(cmd)
        lines = []
        if self.tag_ref in cmd:
            lines.append(f"{self.tag_obj_sha}\t{self.tag_ref}")
        if self.peeled_ref in cmd:  # only present when the peel is explicitly queried
            lines.append(f"{self.commit_sha}\t{self.peeled_ref}")
        return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(lines) + "\n")


def test_resolve_annotated_tag_returns_commit_not_tag_object():
    # Regression: an annotated tag must resolve to its underlying COMMIT, never the
    # tag-object sha. resolve_to_sha must query the peeled ref so the `^{}` row exists.
    runner = _RealisticLsRemote(tag="v1.2.3", tag_obj_sha="a" * 40, commit_sha="b" * 40)
    assert r.resolve_to_sha("v1.2.3", runner=runner) == "b" * 40
    # the peel ref MUST be in the query, else the commit row never comes back
    assert any("refs/tags/v1.2.3^{}" in call for call in runner.calls)


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


def test_resolve_lowercases_an_uppercase_sha():
    # image tags use the lowercase sha, so DEADBEEF must normalize to deadbeef.
    runner = FakeRunner(stdout="should-not-be-used")
    assert r.resolve_to_sha("DEADBEEF", runner=runner) == "deadbeef"
    assert runner.calls == []  # still no remote lookup for a bare sha


def test_resolve_timeout_is_wrapped_as_value_error():
    runner = FakeRunner(raises=subprocess.TimeoutExpired(["git", "ls-remote"], 30))
    with pytest.raises(ValueError, match="git ls-remote failed"):
        r.resolve_to_sha("main", runner=runner)


def test_redact_repo_strips_embedded_credentials():
    assert (
        r._redact_repo("https://ghp_secret@github.com/x/y.git")
        == "https://<redacted>@github.com/x/y.git"
    )
    assert (
        r._redact_repo("https://user:pass@github.com/x/y.git")
        == "https://<redacted>@github.com/x/y.git"
    )
    # an unauthenticated URL is left untouched
    assert r._redact_repo("https://github.com/x/y.git") == "https://github.com/x/y.git"


def test_error_messages_do_not_leak_repo_credentials():
    runner = FakeRunner(stdout="")  # ref not found
    authed = "https://ghp_secrettoken@github.com/wangzitian0/finance_report.git"
    with pytest.raises(ValueError) as exc:
        r.resolve_to_sha("v9.9.9", repo=authed, runner=runner)
    assert "ghp_secrettoken" not in str(exc.value)
    assert "<redacted>@" in str(exc.value)


def test_subprocess_error_text_does_not_leak_repo_credentials():
    # CalledProcessError/TimeoutExpired str() embeds the FULL command — including the
    # authenticated repo URL. The wrapped ValueError must redact the exception text
    # too, not just the surrounding message (Copilot CR).
    authed = "https://ghp_secrettoken@github.com/wangzitian0/finance_report.git"
    exc = subprocess.CalledProcessError(
        128, ["git", "ls-remote", authed, "refs/heads/main"]
    )
    runner = FakeRunner(raises=exc)
    with pytest.raises(ValueError) as ei:
        r.resolve_to_sha("main", repo=authed, runner=runner)
    assert "ghp_secrettoken" not in str(ei.value)
    assert "<redacted>@" in str(ei.value)


def test_timeout_error_text_does_not_leak_repo_credentials():
    authed = "https://ghp_secrettoken@github.com/wangzitian0/finance_report.git"
    exc = subprocess.TimeoutExpired(["git", "ls-remote", authed], 30)
    runner = FakeRunner(raises=exc)
    with pytest.raises(ValueError) as ei:
        r.resolve_to_sha("main", repo=authed, runner=runner)
    assert "ghp_secrettoken" not in str(ei.value)


def test_wrapped_git_error_suppresses_exception_chaining():
    # `from None`: the original CalledProcessError keeps the unredacted repo URL in its
    # args, so it must NOT be attached as __cause__ — otherwise traceback/chained output
    # would re-leak the token even though our message is redacted (Copilot CR).
    authed = "https://ghp_secrettoken@github.com/x/y.git"
    exc = subprocess.CalledProcessError(128, ["git", "ls-remote", authed])
    runner = FakeRunner(raises=exc)
    with pytest.raises(ValueError) as ei:
        r.resolve_to_sha("main", repo=authed, runner=runner)
    assert ei.value.__cause__ is None
    assert ei.value.__suppress_context__ is True


# --- resolve_image_ref: form decides the image (sha7 for code, tag for release) ---


def test_resolve_image_ref_sha_uses_short_sha():
    rr = r.resolve_image_ref(
        "a" * 40, runner=FakeRunner(raises=AssertionError("no git"))
    )
    assert (rr.sha, rr.image_ref, rr.form) == ("a" * 40, "aaaaaaa", "sha")


def test_resolve_image_ref_main_uses_short_sha():
    runner = FakeRunner(stdout="1" * 40 + "\trefs/heads/main\n")
    rr = r.resolve_image_ref("main", runner=runner)
    assert rr.image_ref == "1111111" and rr.sha == "1" * 40 and rr.form == "branch"


def test_resolve_image_ref_tag_uses_the_tag():
    runner = FakeRunner(
        stdout="tagobj\trefs/tags/v1.2.3\n" + "3" * 40 + "\trefs/tags/v1.2.3^{}\n"
    )
    rr = r.resolve_image_ref("v1.2.3", runner=runner)
    # release artifacts are pulled BY TAG (the retained image), sha stays the identity
    assert rr.image_ref == "v1.2.3" and rr.sha == "3" * 40 and rr.form == "tag"


def test_resolve_pr_uses_pull_head_short_sha():
    runner = FakeRunner(stdout="9" * 40 + "\trefs/pull/7/head\n")
    rr = r.resolve_pr(7, runner=runner)
    assert rr.sha == "9" * 40 and rr.image_ref == "9999999" and rr.form == "pr"


def test_resolve_pr_rejects_bad_number():
    import pytest

    with pytest.raises(ValueError, match="positive integer"):
        r.resolve_pr("abc", runner=FakeRunner())
    with pytest.raises(ValueError, match="positive integer"):
        r.resolve_pr(0, runner=FakeRunner())
