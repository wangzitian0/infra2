#!/usr/bin/env python3
"""Commit-addressed deploy surface resolver — the ``code`` axis of deploy(env, code, data).

The finance_report deploy primitive (finance_report#883) accepts a multi-input surface
that all collapses to ONE commit sha — the published image tag Infra pulls:

    main          -> finance_report main branch HEAD
    release/x.y   -> that release branch HEAD
    vX.Y.Z        -> that tag
    <sha>         -> itself (used verbatim)

This module owns ONLY resolution (input -> sha). It performs no deploy and has no
Dokploy/Vault side effects, so it is a pure, independently testable foundation for the
primitive. The App publishes ``:<sha>`` images for main + release commits (P1a, #879);
this resolver is how Infra turns a human-facing surface input into that sha.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

FINANCE_REPORT_REPO = "https://github.com/wangzitian0/finance_report.git"

_SHA_RE = re.compile(r"\A[0-9a-f]{7,40}\Z")
_TAG_RE = re.compile(r"\Av\d+\.\d+\.\d+\Z")
_RELEASE_BRANCH_RE = re.compile(r"\Arelease/\d+\.\d+\Z")


def classify_ref(ref: str) -> str:
    """Classify a deploy surface input into one of: tag, release-branch, branch, sha.

    Pure and side-effect free. Raises ValueError for shapes outside the surface.
    """
    cleaned = ref.strip()
    if not cleaned:
        raise ValueError("deploy ref must be non-empty")
    if _TAG_RE.match(cleaned):
        return "tag"
    if _RELEASE_BRANCH_RE.match(cleaned):
        return "release-branch"
    if cleaned == "main":
        return "branch"
    if _SHA_RE.match(cleaned):
        return "sha"
    raise ValueError(
        f"unrecognized deploy ref {ref!r}: expected 'main', 'release/x.y', "
        "'vX.Y.Z', or a commit sha"
    )


def _remote_ref_for(kind: str, cleaned: str) -> str:
    return {
        "branch": "refs/heads/main",
        "release-branch": f"refs/heads/{cleaned}",
        "tag": f"refs/tags/{cleaned}",
    }[kind]


def _ls_remote_sha(repo: str, remote_ref: str, *, runner=subprocess.run) -> str | None:
    """Return the sha ``remote_ref`` points to in ``repo``, or None if absent."""
    result = runner(
        ["git", "ls-remote", repo, remote_ref],
        capture_output=True,
        text=True,
        check=True,
    )
    output = (result.stdout or "").strip()
    if not output:
        return None
    return output.splitlines()[0].split("\t", 1)[0].strip() or None


def resolve_to_sha(
    ref: str, *, repo: str = FINANCE_REPORT_REPO, runner=subprocess.run
) -> str:
    """Resolve a deploy surface input to a full commit sha.

    A bare ``<sha>`` is returned verbatim (the caller already addressed a commit).
    main / release/x.y / vX.Y.Z are resolved against ``repo`` via ``git ls-remote``.
    Raises ValueError if the ref shape is unknown or the ref does not exist in repo.
    """
    kind = classify_ref(ref)
    cleaned = ref.strip()
    if kind == "sha":
        return cleaned
    remote_ref = _remote_ref_for(kind, cleaned)
    sha = _ls_remote_sha(repo, remote_ref, runner=runner)
    if not sha:
        raise ValueError(f"deploy ref {ref!r} ({remote_ref}) not found in {repo}")
    return sha


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ref", help="main | release/x.y | vX.Y.Z | <sha>")
    parser.add_argument("--repo", default=FINANCE_REPORT_REPO)
    args = parser.parse_args(argv)
    try:
        print(resolve_to_sha(args.ref, repo=args.repo))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
