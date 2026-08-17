from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from libs.harness_status import workspace_status


def test_workspace_status_exposes_pin_remote_release_and_drift(tmp_path: Path) -> None:
    app = tmp_path / "repos" / "app"
    app.mkdir(parents=True)
    root_sha = "a" * 40
    app_sha = "b" * 40
    remote_app_sha = "c" * 40
    manifest = {
        "repositories": [
            {
                "id": "root",
                "path": ".",
                "checkout": "root",
                "release_identity": "tag",
            },
            {
                "id": "app",
                "path": "repos/app",
                "checkout": "submodule",
                "release_identity": "image",
            },
        ]
    }

    def runner(argv, **_kwargs):
        checkout = Path(argv[2])
        args = argv[3:]
        is_app = checkout == app
        if args == ["rev-parse", "--is-inside-work-tree"]:
            output = "true\n"
        elif args == ["fetch", "--prune", "--tags", "origin"]:
            output = ""
        elif args == ["rev-parse", "HEAD^{commit}"]:
            output = f"{app_sha if is_app else root_sha}\n"
        elif args == ["symbolic-ref", "--quiet", "--short", "HEAD"]:
            return (
                subprocess.CompletedProcess(argv, 1, "", "")
                if is_app
                else subprocess.CompletedProcess(argv, 0, "main\n", "")
            )
        elif args[:4] == [
            "rev-parse",
            "--abbrev-ref",
            "--symbolic-full-name",
            "@{upstream}",
        ]:
            output = "origin/main\n"
        elif args == ["rev-parse", "origin/main^{commit}"]:
            output = f"{remote_app_sha if is_app else root_sha}\n"
        elif args[:3] == ["rev-list", "--left-right", "--count"]:
            output = "0 1\n" if is_app else "0 0\n"
        elif args == ["status", "--porcelain"]:
            output = " M local.txt\n" if is_app else ""
        elif args == ["describe", "--tags", "--always"]:
            output = "v2.0.0\n" if is_app else "v1.0.0\n"
        elif args == ["ls-tree", "HEAD", "--", "repos/app"]:
            output = f"160000 commit {app_sha}\trepos/app\n"
        else:
            raise AssertionError(f"unexpected git call: {argv}")
        return subprocess.CompletedProcess(argv, 0, output, "")

    result = workspace_status(tmp_path, manifest, fetch=True, runner=runner)

    root, app_status = result.repositories
    assert root.current is True
    assert app_status.parent_pin == app_sha
    assert app_status.pin_matches is True
    assert app_status.remote_head == remote_app_sha
    assert app_status.behind == 1
    assert app_status.dirty_paths == 1
    assert app_status.checkout_release == "v2.0.0"
    assert app_status.current is False
    assert result.ok is True
    assert result.current is False


def test_workspace_status_never_reports_clean_when_git_status_fails(
    tmp_path: Path,
) -> None:
    manifest = {
        "repositories": [
            {
                "id": "root",
                "path": ".",
                "checkout": "root",
                "release_identity": "tag",
            }
        ]
    }

    def runner(argv, **_kwargs):
        args = argv[3:]
        values = {
            ("rev-parse", "--is-inside-work-tree"): "true\n",
            ("rev-parse", "HEAD^{commit}"): "a" * 40 + "\n",
            ("symbolic-ref", "--quiet", "--short", "HEAD"): "main\n",
            (
                "rev-parse",
                "--abbrev-ref",
                "--symbolic-full-name",
                "@{upstream}",
            ): "origin/main\n",
            ("rev-parse", "origin/main^{commit}"): "a" * 40 + "\n",
            (
                "rev-list",
                "--left-right",
                "--count",
                "HEAD...origin/main",
            ): "0 0\n",
            ("describe", "--tags", "--always"): "v1.0.0\n",
        }
        if args == ["status", "--porcelain"]:
            return subprocess.CompletedProcess(argv, 128, "", "index unavailable")
        return subprocess.CompletedProcess(argv, 0, values[tuple(args)], "")

    result = workspace_status(tmp_path, manifest, runner=runner)
    status = result.repositories[0]

    assert status.dirty_paths is None
    assert "working-tree status failed" in status.error
    assert status.current is False
    assert result.ok is False


@pytest.mark.parametrize(
    ("option", "expected"), [(None, True), ("--no-submodules-expected", False)]
)
def test_status_passes_submodule_expectation_to_manifest_validation(
    monkeypatch, tmp_path: Path, option: str | None, expected: bool
) -> None:
    import tools.harness as harness

    observed = []

    def check_workspace(_root, _manifest, *, submodules_expected=True):
        observed.append(submodules_expected)
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(harness, "check_workspace", check_workspace)
    monkeypatch.setattr(harness, "load_manifest", lambda _path: {"repositories": []})
    monkeypatch.setattr(
        harness,
        "workspace_status",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, current=False),
    )
    monkeypatch.setattr(harness, "_print_status", lambda _result: None)
    argv = ["status", "--root", str(tmp_path)]
    if option:
        argv.append(option)

    assert harness.main(argv) == 0
    assert observed == [expected]
