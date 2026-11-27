"""The App pin check of a pull request (#1644 Q2): the train ``pre`` checks, before the merge."""

from __future__ import annotations

import subprocess

import pytest

from common.deploy import app_pin_check
from common.deploy.reconcile_iac_inputs import UnverifiedSdkError


def _git(root, *argv: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *argv],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A repository whose base commit holds the tags fr-1 and ta-1, with ta-2 in the work tree."""
    tags = {"finance_report/app": "fr-1", "truealpha/app": "ta-1"}
    monkeypatch.setattr(
        "common.deploy.reconcile_iac_inputs._parse_repos_yaml_release_tags",
        lambda text: {
            svc: tag
            for svc, tag in (line.split("=") for line in text.split() if "=" in line)
        },
    )
    harness = tmp_path / "harness"
    harness.mkdir()
    _git(tmp_path, "init", "-q")
    (harness / "repos.yaml").write_text(
        " ".join(f"{svc}={tag}" for svc, tag in tags.items())
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    tags["truealpha/app"] = "ta-2"
    (harness / "repos.yaml").write_text(
        " ".join(f"{svc}={tag}" for svc, tag in tags.items())
    )
    return tmp_path


def test_moved_pins_names_only_the_pin_that_differs_from_the_base(checkout) -> None:
    assert app_pin_check.moved_pins(checkout, "HEAD") == {"truealpha/app": "ta-2"}


def test_an_unreadable_base_is_an_error_and_not_no_pin_moved(checkout, capsys) -> None:
    with pytest.raises(ValueError, match="not readable at 'no-such-commit'"):
        app_pin_check.moved_pins(checkout, "no-such-commit")
    assert app_pin_check.main(["--base", "no-such-commit"], root=checkout) == 1
    assert "::error::app pin check:" in capsys.readouterr().out


def _raise(exc):
    def call(*_args, **_kwargs):
        raise exc

    return call


def _passes(*_args, **_kwargs) -> None:
    return None


def test_a_pin_whose_sdk_has_no_canary_pass_fails_before_the_merge(
    checkout, capsys, monkeypatch
) -> None:
    """Release v3.2.64: truealpha/app@v0.3.24 locked 4.14.0; the train refused it in ``pre``."""
    refusal = UnverifiedSdkError("truealpha/app@ta-2 locks an SDK with no Canary pass")
    monkeypatch.setattr(
        app_pin_check,
        "check_pins",
        lambda pins, *, root: _original(
            pins, root=root, on_main=_passes, sdk_verified=_raise(refusal)
        ),
    )
    assert app_pin_check.main(["--base", "HEAD"], root=checkout) == 1
    out = capsys.readouterr().out
    assert "the Staging train refuses this pin: truealpha/app@ta-2 locks" in out
    assert app_pin_check.CANARY_FIRST in out


def test_a_tag_that_is_off_the_app_main_fails(checkout) -> None:
    refusals, unread = _original(
        {"truealpha/app": "ta-2"},
        root=checkout,
        on_main=_raise(ValueError("version_ref 'ta-2' is not on main")),
        sdk_verified=_passes,
    )
    assert refusals == ["truealpha/app@ta-2: version_ref 'ta-2' is not on main"]
    assert unread == []


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("stage_records: GitHub rate limit"),
        ValueError("failed to fetch wangzitian0/truealpha:uv.lock@ta-2"),
        OSError("network is unreachable"),
    ],
)
def test_an_unreadable_source_prints_skipped_and_passes(
    checkout, capsys, monkeypatch, error
) -> None:
    """The ``pre`` phase stays the gate; a source that is not readable is not a refusal."""
    monkeypatch.setattr(
        app_pin_check,
        "check_pins",
        lambda pins, *, root: _original(
            pins, root=root, on_main=_passes, sdk_verified=_raise(error)
        ),
    )
    assert app_pin_check.main(["--base", "HEAD"], root=checkout) == 0
    out = capsys.readouterr().out
    assert (
        f"::warning::app pin check: SKIPPED, a source is not readable: infra2-sdk versions: {error}"
        in out
    )
    assert "app pin check: SKIPPED (truealpha/app@ta-2)" in out
    assert "app pin check: ok" not in out


def test_each_moved_pin_goes_to_both_train_checks(
    checkout, capsys, monkeypatch
) -> None:
    seen: list[object] = []
    monkeypatch.setattr(
        app_pin_check,
        "check_pins",
        lambda pins, *, root: _original(
            pins,
            root=root,
            on_main=lambda tag, deploy_type, *, service: seen.append(
                (service, tag, deploy_type)
            ),
            sdk_verified=lambda pins, *, repo_root: seen.append(pins),
        ),
    )
    assert app_pin_check.main(["--base", "HEAD"], root=checkout) == 0
    assert seen == [
        ("truealpha/app", "ta-2", "staging"),
        [("truealpha/app", "ta-2")],
    ]
    assert "app pin check: ok (truealpha/app@ta-2)" in capsys.readouterr().out


_original = app_pin_check.check_pins
