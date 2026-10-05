"""Commit-addressed app ref resolution for deploy_v2 ``version_ref`` inputs.

The finance_report app deploy surface accepts a multi-input ``version_ref`` that all
collapses to ONE commit sha plus the published image ref deploy_v2 asks Infra to pull:

    main          -> finance_report main branch HEAD
    vX.Y.Z        -> that tag
    <sha>         -> itself (used verbatim)

This module owns ONLY resolution (input -> sha). It delegates Git ref classification,
peeling, and remote resolution to ``infra2_sdk.refs`` while retaining the finance_report
default repository binding and Dokploy-specific clone branch validation.

It is library code: ``libs.deploy.preview`` / ``libs.deploy.promote`` import it from here
instead of reaching up into ``tools/`` (#955). ``tools/resolve_deploy_ref.py`` keeps the
CLI front door and re-exports these names for the callers that still import them there.
"""

from __future__ import annotations

import subprocess

from infra2_sdk.refs import (
    CommandRunner,
    ResolvedRef,
    classify_ref,
    ls_remote_rows,
    redact_repo,
    resolve_image_ref as _sdk_resolve_image_ref,
    resolve_pr as _sdk_resolve_pr,
    resolve_to_sha as _sdk_resolve_to_sha,
)

FINANCE_REPORT_REPO = "https://github.com/wangzitian0/finance_report.git"

__all__ = [
    "CommandRunner",
    "FINANCE_REPORT_REPO",
    "ResolvedRef",
    "classify_ref",
    "resolve_branch_to_sha",
    "resolve_image_ref",
    "resolve_pr",
    "resolve_to_sha",
]


def resolve_to_sha(
    ref: str, *, repo: str = FINANCE_REPORT_REPO, runner: CommandRunner = subprocess.run
) -> str:
    """Resolve a deploy surface input to a commit sha."""
    return _sdk_resolve_to_sha(ref, repo=repo, runner=runner)


def resolve_image_ref(
    ref: str, *, repo: str = FINANCE_REPORT_REPO, runner: CommandRunner = subprocess.run
) -> ResolvedRef:
    """Resolve a surface ref to its (sha identity, image_ref, form)."""
    return _sdk_resolve_image_ref(ref, repo=repo, runner=runner)


def resolve_pr(
    pr_number: int | str,
    *,
    repo: str = FINANCE_REPORT_REPO,
    runner: CommandRunner = subprocess.run,
) -> ResolvedRef:
    """Resolve a PR number to its head commit image (``refs/pull/<N>/head``)."""
    return _sdk_resolve_pr(pr_number, repo=repo, runner=runner)


def resolve_branch_to_sha(
    branch: str,
    *,
    repo: str = FINANCE_REPORT_REPO,
    runner: CommandRunner = subprocess.run,
) -> str:
    """Resolve a clone-only branch without broadening the deploy authority grammar.

    Preview PRs need a branch name because Dokploy cannot clone a raw commit SHA. The
    exact SHA remains authoritative; callers use this resolver only to prove the transport
    branch points at that same commit before handing it to Dokploy.
    """
    cleaned = branch.strip()
    invalid_chars = " ~^:?*[\x5c"
    if (
        not cleaned
        or cleaned.startswith(("-", ".", "/", "refs/"))
        or cleaned.endswith((".", "/", ".lock"))
        or ".." in cleaned
        or "@{" in cleaned
        or "//" in cleaned
        or any(ord(char) < 32 or char in invalid_chars for char in cleaned)
    ):
        raise ValueError(f"invalid clone branch {branch!r}")

    remote_ref = f"refs/heads/{cleaned}"
    for sha, name in ls_remote_rows(repo, remote_ref, runner=runner):
        if name == remote_ref:
            return sha
    raise ValueError(
        f"clone branch {branch!r} ({remote_ref}) not found in {redact_repo(repo)}"
    )
