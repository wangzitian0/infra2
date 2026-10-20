"""infra2_sdk.refs release markers: the two coordinates, read from git (#1270)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from common.deploy import release_markers

from common.deploy import app_deploy_request_cli as app_deploy_request

ROOT = Path(__file__).resolve().parents[2]


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo_with_releases(path: Path, *, marker: str, releases: tuple[str, ...]) -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "test", cwd=path)
    (path / "service.py").write_text("x\n")
    _git("add", "-A", cwd=path)
    _git("commit", "-qm", "first", cwd=path)
    for tag in releases:
        _git("tag", "-a", tag, "-m", tag, cwd=path)
    _git("tag", "-a", f"production/{marker}", "-m", "promoted", cwd=path)
    return path


def test_the_report_reads_both_coordinates_out_of_a_real_repository(tmp_path):
    repo = _repo_with_releases(
        tmp_path / "infra2", marker="v1.1.76", releases=("v1.1.76", "v1.1.77")
    )
    status = release_markers.marker_status(repo_root=repo)
    assert (status.production_marker, status.newest_tag) == ("v1.1.76", "v1.1.77")
    assert status.releases_behind == 1
    assert not status.stale
    assert status.line() == (
        "production marker v1.1.76 (1 release(s) behind); newest release v1.1.77"
    )


def test_a_marker_older_than_the_release_pin_reads_as_stale(tmp_path):
    repo = _repo_with_releases(
        tmp_path / "infra2", marker="v1.1.52", releases=("v1.1.52", "v1.1.76")
    )
    status = release_markers.marker_status(repo_root=repo)
    assert status.stale
    assert "#632" in status.line() and "#650" in status.line()


def test_the_markers_action_prints_coordinates_from_git(tmp_path, capsys):
    repo = _repo_with_releases(
        tmp_path / "infra2", marker="v1.1.76", releases=("v1.1.76", "v1.1.77")
    )
    assert app_deploy_request.main(["markers", "--repo-root", str(repo)]) == 0
    assert "production marker v1.1.76" in capsys.readouterr().out
