"""Prove the isolated iac-runner container (/app) boots without repo-root packages."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from common.deploy.git_refs import classify_ref as repo_classify_ref

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "bootstrap/06.iac_runner/Dockerfile"


def _copy_dockerfile_app_layout(dest: Path) -> None:
    """Replicate Dockerfile COPY sources under dest using the same relative targets."""
    copy_re = re.compile(r"^COPY\s+(\S+)\s+(\S+)\s*$")
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        match = copy_re.match(line.strip())
        if not match:
            continue
        src_rel, dst_rel = match.groups()
        if src_rel == "bootstrap/06.iac_runner/requirements.txt" or src_rel == "uv.lock":
            continue
        src = ROOT / src_rel
        target = dest / (Path(src_rel).name if dst_rel == "." else dst_rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)


def test_iac_runner_app_imports_without_repo_root(tmp_path: Path) -> None:
    """webhook_server and sync_runner must import in an isolated /app without common."""
    _copy_dockerfile_app_layout(tmp_path)
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("PYTHONPATH", "PYTHONHOME")
    }
    env["PYTHONPATH"] = str(tmp_path)
    env["GIT_REPO_URL"] = "https://github.com/wangzitian0/infra2"

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, webhook_server, sync_runner; "
            "assert 'common' not in sys.modules, list(sys.modules); "
            "assert webhook_server.classify_ref('v1.2.3') == 'tag'",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"


@pytest.mark.parametrize(
    "raw_ref",
    [
        "main",
        "v4.11.2",
        "pr-42",
        "123",
        123,
        "a1b2c3d",
        "abcdef1234567890abcdef1234567890abcdef12",
        "",
        "   ",
        "feature/foo",
        "0",
        "pr-0",
        "v1.2",
    ],
)
def test_webhook_server_classify_ref_matches_git_refs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw_ref: str | int
) -> None:
    """Inlined classify_ref in webhook_server matches common.deploy.git_refs."""
    import importlib.util

    monkeypatch.setenv("GIT_REPO_URL", "https://github.com/wangzitian0/infra2")
    _copy_dockerfile_app_layout(tmp_path)
    spec = importlib.util.spec_from_file_location(
        "isolated_webhook_server", tmp_path / "webhook_server.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    try:
        expected = repo_classify_ref(raw_ref)
    except ValueError:
        with pytest.raises(ValueError):
            module.classify_ref(raw_ref)
    else:
        assert module.classify_ref(raw_ref) == expected
