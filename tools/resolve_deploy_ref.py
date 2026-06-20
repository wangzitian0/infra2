#!/usr/bin/env python3
"""Commit-addressed app ref resolver for deploy_v2 ``version_ref`` inputs.

The finance_report app deploy surface accepts a multi-input ``version_ref`` that all
collapses to ONE commit sha plus the published image ref deploy_v2 asks Infra to pull:

    main          -> finance_report main branch HEAD
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
from dataclasses import dataclass

FINANCE_REPORT_REPO = "https://github.com/wangzitian0/finance_report.git"

_SHA_RE = re.compile(r"\A[0-9a-fA-F]{7,40}\Z")
_TAG_RE = re.compile(r"\Av\d+\.\d+\.\d+\Z")

# git ls-remote is a network op; cap it so a stalled DNS/connection surfaces as a
# wrapped ValueError instead of hanging the resolver (and any deploy pipeline).
_LS_REMOTE_TIMEOUT_SECONDS = 30


def _redact_repo(repo: str) -> str:
    """Redact ``user:pass@`` / ``<token>@`` userinfo so an authenticated repo URL
    cannot leak credentials into error messages or logs."""
    return re.sub(r"(://)[^/@\s]+@", r"\1<redacted>@", repo)


def classify_ref(ref: str) -> str:
    """Classify a deploy surface input into one of: tag, branch, sha.

    Pure and side-effect free. Raises ValueError for shapes outside the surface.
    """
    cleaned = ref.strip()
    if not cleaned:
        raise ValueError("deploy ref must be non-empty")
    if _TAG_RE.match(cleaned):
        return "tag"
    if cleaned == "main":
        return "branch"
    if _SHA_RE.match(cleaned):
        return "sha"
    raise ValueError(
        f"unrecognized deploy ref {ref!r}: expected 'main', 'vX.Y.Z', or a commit sha"
    )


def _remote_ref_for(kind: str, cleaned: str) -> str:
    return {
        "branch": "refs/heads/main",
        "tag": f"refs/tags/{cleaned}",
    }[kind]


def _ls_remote_rows(
    repo: str, remote_ref: str, *, runner=subprocess.run
) -> list[tuple[str, str]]:
    """Return ``[(sha, ref_name), ...]`` from ``git ls-remote repo remote_ref``.

    Subprocess failures (git missing, network/auth, non-zero exit) are wrapped as
    ValueError so callers get a stable error contract instead of a raw traceback.
    """
    try:
        result = runner(
            ["git", "ls-remote", repo, remote_ref],
            capture_output=True,
            text=True,
            check=True,
            timeout=_LS_REMOTE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # str(exc) for CalledProcessError/TimeoutExpired embeds the FULL command —
        # including the raw repo arg, which may carry an authenticated URL/token.
        # Redact the exception text too, not just the surrounding message — and use
        # `from None` to suppress chaining: the original exception keeps the raw repo
        # URL in its args, which would still leak via __cause__/traceback output even
        # though our message is redacted.
        raise ValueError(
            f"git ls-remote failed for {remote_ref!r} in {_redact_repo(repo)}: "
            f"{_redact_repo(str(exc))}"
        ) from None
    rows: list[tuple[str, str]] = []
    for line in (result.stdout or "").strip().splitlines():
        sha, _, name = line.partition("\t")
        if sha.strip() and name.strip():
            rows.append((sha.strip(), name.strip()))
    return rows


def _resolve_remote_sha(
    repo: str, kind: str, cleaned: str, *, runner=subprocess.run
) -> str | None:
    """Resolve a non-sha kind to a commit sha via ls-remote.

    Annotated tags are peeled to their underlying commit (``refs/tags/<tag>^{}``):
    finance_report ships annotated tags, so the bare tag ref would otherwise return
    the tag-object sha, not the commit used as the published image tag.
    """
    remote_ref = _remote_ref_for(kind, cleaned)
    rows = _ls_remote_rows(repo, remote_ref, runner=runner)
    if kind == "tag":
        peeled = remote_ref + "^{}"
        for sha, name in rows:
            if name == peeled:  # annotated tag -> underlying commit
                return sha
    for sha, name in rows:
        if name == remote_ref:  # branch, lightweight tag, or fallback
            return sha
    return None


def resolve_to_sha(
    ref: str, *, repo: str = FINANCE_REPORT_REPO, runner=subprocess.run
) -> str:
    """Resolve a deploy surface input to a commit sha.

    A bare ``<sha>`` is returned as-is apart from lowercasing — the caller already
    addressed a commit and may use the short or upper-case form ``classify_ref``
    accepts; it is lowercased (image tags use the lowercase sha) but not expanded to a
    full sha. main / vX.Y.Z are resolved against ``repo`` via ``git ls-remote``, with
    annotated tags peeled to the underlying commit. Raises ValueError if the ref shape
    is unknown, the ref is absent in repo, or git fails.
    """
    kind = classify_ref(ref)
    cleaned = ref.strip()
    if kind == "sha":
        # image tags use the lowercase sha; normalize so DEADBEEF == deadbeef.
        return cleaned.lower()
    sha = _resolve_remote_sha(repo, kind, cleaned, runner=runner)
    if not sha:
        remote_ref = _remote_ref_for(kind, cleaned)
        raise ValueError(
            f"deploy ref {ref!r} ({remote_ref}) not found in {_redact_repo(repo)}"
        )
    return sha


@dataclass(frozen=True)
class ResolvedRef:
    """What a surface ref resolves to: its commit identity AND what image to pull.

    The IMAGE_REF (not the type) is decided by the FORM:
      code  (``branch`` main / ``sha``)  -> the short-sha image  (App publishes :<sha7>)
      release (``tag`` vX.Y.Z)            -> the tag image (retained)
    ``sha`` is always the full/given commit (the canonical identity); ``image_ref`` is what
    Dokploy is told to pull. This is the App's publish contract — deploy_v2 just consumes
    ``image_ref`` and never re-derives sha-vs-tag.
    """

    sha: str  # commit identity (full for resolved refs; as-given for a bare sha)
    image_ref: str  # what to pull: <sha7> for code, the tag for release
    form: str  # classify_ref form: branch | sha | tag


def resolve_image_ref(
    ref: str, *, repo: str = FINANCE_REPORT_REPO, runner=subprocess.run
) -> ResolvedRef:
    """Resolve a surface ref to its (sha identity, image_ref, form).

    - ``tag`` (vX.Y.Z)          -> image_ref = the tag (the App's retained release image).
    - ``branch`` (main)/``sha`` -> image_ref = the 7-char short sha (App's :<sha7> image).
    """
    form = classify_ref(ref)
    cleaned = ref.strip()
    if form == "tag":
        sha = _resolve_remote_sha(repo, "tag", cleaned, runner=runner)
        if not sha:
            raise ValueError(f"tag {cleaned!r} not found in {_redact_repo(repo)}")
        return ResolvedRef(sha=sha, image_ref=cleaned, form=form)
    sha = resolve_to_sha(ref, repo=repo, runner=runner)  # branch (main) / sha
    return ResolvedRef(sha=sha, image_ref=sha[:7], form=form)


def resolve_pr(
    pr_number, *, repo: str = FINANCE_REPORT_REPO, runner=subprocess.run
) -> ResolvedRef:
    """Resolve a PR number to its head commit image (``refs/pull/<N>/head``).

    preview/pr's ``version_ref`` is a PR number (not a git ref classify_ref understands):
    its slot is ``pr-<N>`` and its code is the PR head commit, pulled by short sha.
    """
    n = str(pr_number).strip()
    if not (n.isdigit() and int(n) > 0):
        raise ValueError(f"PR number must be a positive integer, got {pr_number!r}")
    ref = f"refs/pull/{n}/head"
    for sha, name in _ls_remote_rows(repo, ref, runner=runner):
        if name == ref:
            return ResolvedRef(sha=sha, image_ref=sha[:7], form="pr")
    raise ValueError(f"PR #{n} head not found in {_redact_repo(repo)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ref", help="main | vX.Y.Z | <sha>")
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
