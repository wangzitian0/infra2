"""The App pin check of a pull request (#1644 Q2): the train ``pre`` checks, before the merge."""

from __future__ import annotations

import subprocess
from unittest.mock import create_autospec

import pytest

from common.deploy import app_pin_check, preflight, reconcile_iac_inputs
from common.deploy.reconcile_iac_inputs import UnverifiedSdkError

REPOS_YAML = """\
repositories:
  - id: finance-report
    role: external-application
    release_tag: {fr}
  - id: truealpha
    role: external-application
    release_tag: {ta}
"""


def _git(root, *argv: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *argv],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def checkout(tmp_path):
    """A repository whose base commit holds the tags fr-1 and ta-1, with ta-2 in the work tree."""
    harness = tmp_path / "harness"
    harness.mkdir()
    _git(tmp_path, "init", "-q")
    (harness / "repos.yaml").write_text(REPOS_YAML.format(fr="fr-1", ta="ta-1"))
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    (harness / "repos.yaml").write_text(REPOS_YAML.format(fr="fr-1", ta="ta-2"))
    return tmp_path


@pytest.fixture
def train(monkeypatch):
    """Record the calls to the two functions that the train ``pre`` phase calls."""
    calls: list[object] = []
    answers: dict[str, BaseException] = {}

    def on_main(tag, deploy_type, *, service, **_kwargs):
        calls.append((service, tag, deploy_type))
        if "on_main" in answers:
            raise answers["on_main"]

    def sdk_verified(pins, *, repo_root, **_kwargs):
        calls.append(list(pins))
        if "sdk" in answers:
            raise answers["sdk"]

    # autospec: a call that the real signature refuses raises TypeError here too.
    for module, name, fake in (
        (preflight, "assert_app_version_ref_on_main", on_main),
        (reconcile_iac_inputs, "assert_app_sdk_versions_verified", sdk_verified),
    ):
        monkeypatch.setattr(
            module, name, create_autospec(getattr(module, name), side_effect=fake)
        )
    return calls, answers


def test_each_moved_pin_goes_to_the_two_train_checks(checkout, train, capsys) -> None:
    calls, _ = train
    assert app_pin_check.main(["--base", "HEAD"], root=checkout) == 0
    assert calls == [("truealpha/app", "ta-2", "staging"), [("truealpha/app", "ta-2")]]
    assert "app pin check: ok (truealpha/app@ta-2)" in capsys.readouterr().out


def test_no_moved_pin_calls_no_train_check(checkout, train, capsys) -> None:
    calls, _ = train
    _git(checkout, "commit", "-qam", "move the tag")
    assert app_pin_check.main(["--base", "HEAD"], root=checkout) == 0
    assert calls == []
    assert "app pin check: ok, no App pin moves" in capsys.readouterr().out


def test_an_unreadable_base_is_an_error_and_not_no_pin_moved(checkout, capsys) -> None:
    assert app_pin_check.main(["--base", "no-such-commit"], root=checkout) == 1
    assert "not readable at 'no-such-commit'" in capsys.readouterr().out


def test_an_sdk_with_no_canary_pass_fails_before_the_merge(
    checkout, train, capsys
) -> None:
    """Release v3.2.64: the train refused truealpha/app in ``pre`` for this fact."""
    train[1]["sdk"] = UnverifiedSdkError("truealpha/app@ta-2 has no Canary pass")
    assert app_pin_check.main(["--base", "HEAD"], root=checkout) == 1
    out = capsys.readouterr().out
    assert "the Staging train refuses this pin: truealpha/app@ta-2 has no" in out
    assert app_pin_check.CANARY_FIRST in out


def test_a_tag_that_is_off_the_app_main_fails(checkout, train, capsys) -> None:
    train[1]["on_main"] = ValueError("version_ref 'ta-2' is not on main")
    assert app_pin_check.main(["--base", "HEAD"], root=checkout) == 1
    assert (
        "refuses this pin: truealpha/app@ta-2: version_ref" in capsys.readouterr().out
    )


@pytest.mark.parametrize(
    ("check", "error"),
    [
        ("sdk", RuntimeError("stage_records: the artifact listing is unread")),
        ("sdk", ValueError("failed to fetch the lock of truealpha/app")),
        ("sdk", OSError("network is unreachable")),
        ("on_main", RuntimeError("the GitHub compare API failed")),
    ],
)
def test_an_unreadable_source_prints_skipped_and_passes(
    checkout, train, capsys, check, error
) -> None:
    """The ``pre`` phase stays the gate; a source that is not readable is not a refusal."""
    train[1][check] = error
    assert app_pin_check.main(["--base", "HEAD"], root=checkout) == 0
    out = capsys.readouterr().out
    assert "::warning::app pin check: SKIPPED, a source is not readable:" in out
    assert str(error) in out
    assert "app pin check: SKIPPED (truealpha/app@ta-2)" in out
    assert "app pin check: ok" not in out
