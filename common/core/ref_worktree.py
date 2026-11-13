"""Run a command in a temporary detached git worktree of one ref."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


def run_at_ref(
    ref: str,
    args: list[str],
    *,
    root: Path,
    timeout: float,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
) -> subprocess.CompletedProcess:
    """Run ``args`` with the cwd of a detached worktree of ``ref``; remove it after.

    Raises when the worktree cannot be made or when the run exceeds ``timeout``."""
    with tempfile.TemporaryDirectory(prefix="ref-worktree-") as tmp:
        tree = Path(tmp) / "tree"
        subprocess.run(
            ["git", "worktree", "add", "--detach", "--quiet", str(tree), ref],
            cwd=root,
            check=True,
            capture_output=True,
        )
        try:
            return subprocess.run(
                args,
                cwd=tree,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                input=stdin,
            )
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(tree)],
                cwd=root,
                capture_output=True,
            )
