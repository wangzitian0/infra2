"""Falsifiable guards for the census walk and surface-ledger discovery (#1642, #1410).

#1642 class: a silent empty consumer walk classifies live production callers
as `unused`. The walk must fail loudly when git fails, and the surface ledger
must be discovered from the workspace submodule even in default mode.
"""

from __future__ import annotations

import inspect
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from common.core import sdk_usage_census as C


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _case_list_repo_files_raises_when_git_fails(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / ".git").mkdir()
    (tmp_path / "mod.py").write_text("VALUE = 1\n")

    def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        # mimic subprocess.run(check=True): a nonzero exit raises
        raise subprocess.CalledProcessError(
            128, args[0], stderr="fatal: unable to read index.lock"
        )

    monkeypatch.setattr(C.subprocess, "run", fail)
    try:
        C.list_repo_files(tmp_path, suffix=".py")
    except RuntimeError as exc:
        assert "index.lock" in str(exc)
    else:
        raise AssertionError(
            "git failure must raise RuntimeError, not return an empty walk"
        )


def _case_list_repo_files_lists_tracked_python(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "keep.py").write_text("A = 1\n")
    (tmp_path / "skip.txt").write_text("nope\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    files = C.list_repo_files(tmp_path, suffix=".py")
    assert files == ["keep.py"]


def _case_surface_ledger_found_via_workspace_submodule(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    pkg = tmp_path / "repos/infra2-sdk/src/infra2_sdk"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text('__all__ = ["thing"]\n')
    ledger = tmp_path / "repos/infra2-sdk/tests"
    ledger.mkdir(parents=True)
    (ledger / "surface.json").write_text(
        json.dumps({"infra2_sdk": {"thing": "held: seeded reason"}})
    )
    monkeypatch.setattr(C, "REPO_ROOT", tmp_path)
    # repo-checkout layout candidate misses (no tests/ beside src), submodule candidate hits
    out = C.load_surface_justifications(pkg)
    assert out[("infra2_sdk", "thing")]["kind"] == "held"


def _case_m1_reach_pins_a_known_caller_as_prod_used(tmp_path: Path) -> None:
    """A consumer prod import of a package symbol must classify prod_used.

    This pins the #1642 class: the walk seeing the file is the only way this
    passes; a silent empty walk reports unused and fails here.
    """
    pkg = tmp_path / "repos/infra2-sdk/src/infra2_sdk"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        'def thing() -> int:\n    return 1\n\n\n__all__ = ["thing"]\n'
    )
    app = tmp_path / "fake_app"
    app.mkdir()
    (app / ".git").mkdir()
    (app / "use.py").write_text("from infra2_sdk import thing\n\nthing()\n")
    _git(app, "init", "-q")
    _git(app, "add", "-A")
    monkey_consumers = {"fake_app": app}
    m1, _seen = C.compute_m1(pkg, monkey_consumers)
    row = next(s for s in m1["symbols"] if s["name"] == "thing")
    assert row["reach"] == "prod_used", row
    assert "fake_app" in row["prod_repos"]


def _run_cases(tmp_path: Path, capsys: pytest.CaptureFixture[str], *cases) -> None:
    """Run each case in its own directory, with fresh patches and a flushed capture."""
    assert cases, "a group without cases checks nothing"
    for index, case in enumerate(cases):
        base = tmp_path / f"{index}_{case.__name__}"
        base.mkdir()
        capsys.readouterr()
        params = inspect.signature(case).parameters
        kwargs: dict[str, Any] = {}
        if "tmp_path" in params:
            kwargs["tmp_path"] = base
        if "capsys" in params:
            kwargs["capsys"] = capsys
        with pytest.MonkeyPatch.context() as patch:
            if "monkeypatch" in params:
                kwargs["monkeypatch"] = patch
            case(**kwargs)


def test_the_census_walk_and_surface_ledger_find_what_is_there(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run_cases(
        tmp_path,
        capsys,
        _case_list_repo_files_raises_when_git_fails,
        _case_list_repo_files_lists_tracked_python,
        _case_surface_ledger_found_via_workspace_submodule,
        _case_m1_reach_pins_a_known_caller_as_prod_used,
    )
