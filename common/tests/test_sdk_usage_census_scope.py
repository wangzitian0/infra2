"""Falsifiable guards for the census walk and surface-ledger discovery (#1642, #1410).

#1642 class: a silent empty consumer walk classifies live production callers
as `unused`. The walk must fail loudly when git fails, and the surface ledger
must be discovered from the workspace submodule even in default mode.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from common.core import sdk_usage_census as C


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_list_repo_files_raises_when_git_fails(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
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


def test_list_repo_files_lists_tracked_python(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "keep.py").write_text("A = 1\n")
    (tmp_path / "skip.txt").write_text("nope\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    files = C.list_repo_files(tmp_path, suffix=".py")
    assert files == ["keep.py"]


def test_surface_ledger_found_via_workspace_submodule(
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


def test_m1_reach_pins_a_known_caller_as_prod_used(tmp_path: Path) -> None:
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
