"""libs/release_markers: the two coordinates, read from git and nothing else.

ops-checks run 34430724977 (2026-09-10) failed on
`ModuleNotFoundError: No module named 'infra2_sdk'`: the daily marker report imported
through the deploy-request adapter, and the job that runs it installs invoke/httpx/PyYAML
and no SDK. These tests hold the report to real git and to that import boundary.
"""

from __future__ import annotations

import importlib
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools/app_deploy_request.py"


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
    from libs import release_markers

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
    from libs import release_markers

    repo = _repo_with_releases(
        tmp_path / "infra2", marker="v1.1.52", releases=("v1.1.52", "v1.1.76")
    )
    status = release_markers.marker_status(repo_root=repo)
    assert status.stale
    assert "#632" in status.line() and "#650" in status.line()


def test_the_markers_action_runs_without_the_deploy_sdk(tmp_path, capsys, monkeypatch):
    """The exact failure of run 34430724977, as a test.

    `infra2_sdk` is made unimportable and the tool is loaded fresh: the `markers` action
    must still work, because the coordinates it prints are git facts.
    """
    repo = _repo_with_releases(
        tmp_path / "infra2", marker="v1.1.76", releases=("v1.1.76", "v1.1.77")
    )

    for name in [
        n
        for n in sys.modules
        if n.startswith(("infra2_sdk", "libs.app_deploy_request"))
    ]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "infra2_sdk", None)  # import -> ImportError
    monkeypatch.delitem(sys.modules, "tools.app_deploy_request", raising=False)

    with pytest.raises(ImportError):
        importlib.import_module("infra2_sdk")

    spec = importlib.util.spec_from_file_location("markers_without_sdk", TOOL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["markers_without_sdk"] = module
    spec.loader.exec_module(module)

    assert module.main(["markers", "--repo-root", str(repo)]) == 0
    assert "production marker v1.1.76" in capsys.readouterr().out
