"""Git provenance: is a release ref reachable from reviewed main?

Standard library only, so a CI job without infra2-sdk can import it. The single
implementation for ``tools/reconcile_iac_inputs.py`` (tag-push reconcile) and
``libs.deploy.preflight.assert_iac_ref_on_main`` (its fallback when the GitHub compare
API cannot answer, #616).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path


def assert_after_on_main(
    after: str,
    repo_root: Path,
    *,
    base: str = "origin/main",
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    """Fail-closed provenance guard: the promoted tag MUST be reachable from main.

    A ``v*.*.*`` tag push drives a REAL staging/prod deploy, so the tagged commit must
    be on reviewed ``origin/main``. This enforces the Infra-011 invariant — *iac_pinned
    production reconcile may run automatically only from reviewed infra2 main* — in code,
    and blocks the v1.1.16 incident where a release tag cut on an unmerged, off-main
    feature branch promoted a pre-refactor ref straight to prod. ``--dry-run`` callers
    skip this (plan-only, no deploy). Returns the resolved 40-hex commit.
    """
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
    # `git merge-base --is-ancestor`: 0 = ancestor (on-main, ok), 1 = NOT an ancestor
    # (genuinely off-main), anything else (typically 128) = git error, almost always a
    # missing/unresolvable base ref. Keep these distinct — conflating an unresolvable
    # base with "off-main" sends operators the wrong way (the base just was not fetched).
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
