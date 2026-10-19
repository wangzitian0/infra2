"""Git provenance: is a release ref reachable from reviewed main?

Canonical implementation lives in ``infra2_sdk.refs`` (#1270). Re-exported here
for ``tools/reconcile_iac_inputs.py`` and ``libs.deploy.preflight``, with a
stdlib fallback for bare-interpreter CI jobs and pre-v3.7.0 runner containers.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

try:
    from infra2_sdk.refs import assert_after_on_main
except ImportError:  # pragma: no cover - bare interpreter or pre-v3.7.0 runner image

    def assert_after_on_main(  # type: ignore[misc]
        after: str,
        repo_root: Path,
        *,
        base: str = "origin/main",
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> str:
        """Fail-closed provenance guard: the promoted tag MUST be reachable from main."""
        resolved = runner(
            ["git", "rev-parse", "--verify", f"{after}^{{commit}}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if resolved.returncode != 0:
            raise SystemExit(
                f"::error::cannot resolve {after!r} to a commit "
                f"({resolved.stderr.strip() or 'unknown revision'})."
            )
        sha = resolved.stdout.strip()
        ancestor = runner(
            ["git", "merge-base", "--is-ancestor", sha, base],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if ancestor.returncode == 0:
            return sha
        if ancestor.returncode == 1:
            raise SystemExit(
                f"::error::refusing to reconcile {after!r} ({sha[:12]}): not reachable from "
                f"{base}. Release tags must be cut on reviewed main (Infra-011 invariant). "
                f"Re-cut the tag on main, or pass --dry-run to plan only."
            )
        raise SystemExit(
            f"::error::cannot verify provenance of {after!r}: base ref {base!r} is "
            f"unresolvable ({ancestor.stderr.strip() or 'git error'}). Ensure it exists, e.g. "
            f"`git fetch --no-tags origin +refs/heads/main:refs/remotes/origin/main`."
        )


__all__ = ["assert_after_on_main"]
