"""Commit-addressed app ref resolution for deploy_v2 ``version_ref`` inputs.

The finance_report app deploy surface accepts a multi-input ``version_ref`` that all
collapses to ONE commit sha plus the published image ref deploy_v2 asks Infra to pull:

    main          -> finance_report main branch HEAD
    vX.Y.Z        -> that tag
    <sha>         -> itself (used verbatim)

This module owns ONLY resolution (input -> sha). It delegates Git ref classification,
peeling, and remote resolution to ``infra2_sdk.refs`` while retaining the finance_report
default repository binding and Dokploy-specific clone branch validation.

It is library code: ``common.deploy.preview`` / ``common.deploy.promote`` import it from here
instead of reaching up into ``tools/`` (#955). ``tools/resolve_deploy_ref.py`` provides the
CLI front door and imports ``FINANCE_REPORT_REPO`` and ``resolve_to_sha`` from this module.
"""

from __future__ import annotations

import subprocess

from infra2_sdk.refs import (
    CommandRunner,
    ResolvedRef,
    classify_ref,
    redact_repo,
    resolve_branch_to_sha as _sdk_resolve_branch_to_sha,
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
    "redact_repo",
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
    """Resolve a clone-only branch without broadening the deploy authority grammar."""
    return _sdk_resolve_branch_to_sha(branch, repo=repo, runner=runner)
