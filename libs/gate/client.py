"""GitHub API transport, data collection, and check query helpers."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Sequence

from libs.gate.review import thread_weight
from libs.gate.types import (
    DEFAULT_REPO,
    GH_TIMEOUT_S,
    MAX_REVIEW_THREADS,
    MAX_THREAD_COMMENTS,
    NO_CHECKS_REPORTED,
    HeadFacts,
    Runner,
    _epoch,
    _get_subprocess,
    _is_local_root_repo,
)


def _gh(
    argv: Sequence[str], *, max_retries: int = 3, initial_delay: float = 1.0
) -> str:
    subp = _get_subprocess()
    last_err: Exception | None = None
    delay = initial_delay
    for attempt in range(max_retries):
        try:
            result = subp.run(
                ["gh", *argv],
                capture_output=True,
                text=True,
                check=False,
                stdin=getattr(subp, "DEVNULL", subprocess.DEVNULL),
                timeout=GH_TIMEOUT_S,
            )
            if result.returncode == 0:
                return result.stdout
            err_msg = result.stderr.strip() or "failed"
            if any(
                term in err_msg.lower()
                for term in (
                    "rate limit",
                    "secondary rate",
                    "too many requests",
                    "timed out",
                    "connection reset",
                )
            ):
                if attempt < max_retries - 1:
                    time.sleep(delay)
                    delay *= 2.0
                    continue
            raise RuntimeError(f"gh {' '.join(argv)}: {err_msg}")
        except subp.TimeoutExpired as exc:
            last_err = exc
            if attempt < max_retries - 1:
                time.sleep(delay)
                delay *= 2.0
                continue
            raise RuntimeError(
                f"gh {' '.join(argv)}: no response in {GH_TIMEOUT_S}s"
            ) from exc
    if last_err:
        raise last_err
    raise RuntimeError(f"gh {' '.join(argv)}: failed after {max_retries} attempts")


def _file_at(repo: str, ref: str, path: str, *, gh: Runner | None = None) -> str | None:
    """A file's contents at a ref, or None if it cannot be read."""
    if gh is None:
        gh = _gh
    if not (repo and ref and path):
        return None
    try:
        return gh(
            [
                "api",
                "-H",
                "Accept: application/vnd.github.raw",
                f"repos/{repo}/contents/{path}?ref={ref}",
            ]
        )
    except RuntimeError:
        return None


def _read_checks(number: int, *, repo: str, gh: Runner | None = None) -> str:
    """`gh pr checks --json`, with gh's 'nothing registered yet' read as zero checks."""
    if gh is None:
        gh = _gh
    try:
        return gh(
            [
                "pr",
                "checks",
                str(number),
                "--repo",
                repo,
                "--json",
                "name,state,bucket",
            ]
        )
    except RuntimeError as exc:
        if NO_CHECKS_REPORTED not in str(exc):
            raise
        return "[]"


def _base_changed_files(
    repo: str, head_sha: str, base: str, *, gh: Runner | None = None
) -> tuple[str, ...]:
    """Files the base branch has changed since this head diverged from it."""
    if gh is None:
        gh = _gh
    if not head_sha or not base:
        return ()
    try:
        payload = json.loads(
            gh(
                [
                    "api",
                    f"repos/{repo}/compare/{head_sha}...{base}",
                    "--jq",
                    "{files:[.files[]?.filename]}",
                ]
            )
        )
    except (RuntimeError, json.JSONDecodeError):
        return ()
    return tuple(str(f) for f in payload.get("files") or [])


def _field(view: dict, name: str) -> str:
    """A requested field's value, or ABSENT when it is missing OR empty."""
    from libs.gate.types import ABSENT

    value = view.get(name)
    return str(value) if value else ABSENT


def collect(
    number: int, *, repo: str = DEFAULT_REPO, gh: Runner | None = None
) -> HeadFacts:
    """Read the head's facts through `gh`; nothing here decides."""
    if gh is None:
        gh = _gh

    from libs.gate.self_governance import _proven_tighter, _working_tree_rule_drift

    view = json.loads(
        gh(
            [
                "pr",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "number,state,isDraft,baseRefName,headRefOid,files,changedFiles,commits,id,reviews,"
                "reviewDecision,"
                "mergeable,mergeStateStatus,body",
            ]
        )
    )
    checks = json.loads(_read_checks(number, repo=repo, gh=gh) or "[]")
    owner, name = repo.split("/", 1)
    threads = json.loads(
        gh(
            [
                "api",
                "graphql",
                "-f",
                "query=query{repository(owner:%s,name:%s){pullRequest(number:%d)"
                "{author{__typename login} "
                "reviewThreads(first:%d){totalCount nodes{isResolved "
                "comments(first:%d){nodes{body}}}}}}}"
                % (
                    json.dumps(owner),
                    json.dumps(name),
                    number,
                    MAX_REVIEW_THREADS,
                    MAX_THREAD_COMMENTS,
                ),
            ]
        )
    )
    pull = threads["data"]["repository"]["pullRequest"]
    review_threads = pull["reviewThreads"]
    nodes = review_threads["nodes"]
    # null for a deleted account: read as unknown, which keeps the user rule.
    author = pull.get("author") or {}
    commits = view.get("commits") or []
    last_push = max((_epoch(c["committedDate"]) for c in commits), default=0.0)
    head_sha = str(view.get("headRefOid") or "")
    head_seen = any(str(c.get("oid") or "") == head_sha for c in commits)
    base_changed = (
        _base_changed_files(
            repo,
            str(view.get("headRefOid") or ""),
            str(view.get("baseRefName") or ""),
            gh=gh,
        )
        if str(view.get("state") or "") == "OPEN"
        else ()
    )
    changed = tuple(str(f["path"]) for f in view.get("files") or [])
    proven = (
        _proven_tighter(
            repo,
            str(view.get("baseRefName") or ""),
            str(view.get("headRefOid") or ""),
            changed,
            gh=gh,
        )
        if (str(view.get("state") or "") == "OPEN" and _is_local_root_repo(repo))
        else ()
    )
    drift = (
        _working_tree_rule_drift(repo, str(view.get("baseRefName") or ""), gh=gh)
        if (str(view.get("state") or "") == "OPEN" and _is_local_root_repo(repo))
        else ()
    )
    return HeadFacts(
        repo=repo,
        rule_drift=drift,
        number=int(view["number"]),
        state=str(view.get("state") or ""),
        draft=bool(view.get("isDraft")),
        base=str(view.get("baseRefName") or ""),
        head_sha=str(view.get("headRefOid") or ""),
        files=changed,
        proven_tighter=proven,
        changed_files=int(view.get("changedFiles") or 0),
        review_decision=str(view.get("reviewDecision") or ""),
        absent_fields=tuple(
            name
            for name in (
                "files",
                "changedFiles",
                "commits",
                "mergeable",
                "mergeStateStatus",
            )
            if not view.get(name)
        ),
        last_push_at=last_push if head_seen else 0.0,
        checks=tuple(
            (str(c["name"]), str(c.get("bucket") or c.get("state") or ""))
            for c in checks
        ),
        unresolved_threads=sum(1 for n in nodes if not n.get("isResolved")),
        unresolved_weight=sum(
            thread_weight(
                [
                    c.get("body") or ""
                    for c in ((n.get("comments") or {}).get("nodes") or [])
                ]
            )
            for n in nodes
            if not n.get("isResolved")
        ),
        review_threads_total=int(review_threads.get("totalCount") or len(nodes)),
        node_id=str(view.get("id") or ""),
        mergeable=_field(view, "mergeable"),
        merge_state=_field(view, "mergeStateStatus"),
        base_changed_files=base_changed,
        body=str(view.get("body") or ""),
        author=(str(author.get("__typename") or ""), str(author.get("login") or "")),
        reviews=tuple(
            (
                str((r.get("author") or {}).get("login") or ""),
                str((r.get("commit") or {}).get("oid") or ""),
                _epoch(r["submittedAt"]) if r.get("submittedAt") else 0.0,
            )
            for r in view.get("reviews") or []
        ),
    )
