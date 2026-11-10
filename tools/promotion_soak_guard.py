"""Production promotion guard, soak lookup: does this release tag have a green staging soak?

``reconcile-iac-inputs.yml`` promotes a tag to production only after the push-triggered
run of the same workflow (the staging soak) concluded ``success``. Before #970 the guard
asked ``gh run list --branch <tag>`` once and treated an empty answer as "no soak".
GitHub's branch-filtered run listing is eventually consistent: a run that had just
finished, or one whose branch is a tag, intermittently came back as ``[]`` and the
promotion was refused (2026-10-05, v1.2.10: the same query succeeded a minute later) --
a fail-closed error, but one that paged out of band for a soak that was green.

This tool keeps the guard fail-closed and removes the false positive:

* the soak is identified by what it ran -- the tag's commit (``head_sha``) -- and the run
  must also name the tag (``head_branch``) and be push-triggered. The branch-filtered
  listing is read as well, and either listing may supply the run;
* only an EMPTY or unreadable answer is retried (bounded; the index catches up, a
  verdict does not change). A run that exists decides immediately;
* a pass needs positive evidence. An empty listing, a listing error, a run that has not
  concluded, or any concluded run that is not ``success`` exits 1.

Standard library only: the workflow job installs no dependencies for this step.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlencode

SOAK_WORKFLOW = "reconcile-iac-inputs.yml"
DEFAULT_ATTEMPTS = 3
DEFAULT_DELAY_SECONDS = 15.0
PER_PAGE = 30  # a tag push has one soak run; a handful at most after a re-tag
GH_TIMEOUT_SECONDS = 60

GREEN = "green"
EMPTY = "empty"
NOT_GREEN = "not_green"
STAGING_GATE_FAILED = "staging_gate_failed"
ERROR = "error"

EXIT_GREEN = 0
EXIT_NOT_GREEN = 1


class LookupFailed(RuntimeError):
    """The tag's commit or its runs could not be read. Never read as "no soak yet"."""


class SoakClient(Protocol):
    def commit_of(self, tag: str) -> str: ...

    def runs_by_commit(self, sha: str) -> list[dict]: ...

    def runs_by_branch(self, tag: str) -> list[dict]: ...


@dataclass(frozen=True)
class Verdict:
    state: str
    detail: str = ""
    url: str = ""
    created_at: str = ""
    seen: tuple[dict, ...] = ()
    sha: str = ""

    @property
    def ok(self) -> bool:
        return self.state == GREEN

    @property
    def exit_code(self) -> int:
        return EXIT_GREEN if self.state == GREEN else EXIT_NOT_GREEN

    @property
    def run_url(self) -> str:
        return self.url


SoakVerdict = Verdict


def judge_staging_gate(
    hook_results: Sequence[Mapping[str, Any]],
    required_releases: Mapping[str, str],
) -> tuple[bool, str]:
    """Verify every required ``(service_id, version_ref)`` has a green ``staging_gate`` verdict (#1493).

    Returns ``(True, detail)`` when all required ``staging_gate`` entries exist and
    succeeded with no failed entry for that ``(service_id, version_ref)``. Returns
    ``(False, detail)`` naming any missing or failed ``service_id@version_ref``.
    """
    if not required_releases:
        return True, "no staging_gate releases required"
    missing: list[str] = []
    failed: list[str] = []
    for raw_svc, raw_ver in sorted(required_releases.items()):
        svc = str(raw_svc).strip()
        ver = str(raw_ver).strip()
        matching: list[Mapping[str, Any]] = []
        for entry in hook_results or ():
            if not isinstance(entry, Mapping):
                continue
            if str(entry.get("slot") or "").strip() != "staging_gate":
                continue
            entry_svc = str(
                entry.get("service_id") or entry.get("service") or ""
            ).strip()
            entry_ver = str(entry.get("version_ref") or "").strip()
            if entry_svc == svc and entry_ver == ver:
                matching.append(entry)
        if not matching:
            missing.append(f"{svc}@{ver}")
            continue
        has_true = False
        has_false = False
        for entry in matching:
            raw_ok = entry.get("ok")
            if isinstance(raw_ok, bool):
                is_ok = raw_ok
            else:
                is_ok = str(entry.get("status") or "").strip() == "pass"
            if is_ok:
                has_true = True
            else:
                has_false = True
        if has_false or not has_true:
            failed.append(f"{svc}@{ver}")
    if missing or failed:
        parts: list[str] = []
        if missing:
            parts.append(f"missing staging_gate for {', '.join(missing)}")
        if failed:
            parts.append(f"failed staging_gate for {', '.join(failed)}")
        return False, "; ".join(parts)
    verified = ", ".join(
        f"{svc}@{ver}" for svc, ver in sorted(required_releases.items())
    )
    return True, f"staging_gate passed for {verified}"


def _relevant(runs: list[dict], tag: str, sha: str) -> list[dict]:
    """Push-triggered runs of THIS tag on THIS commit, de-duplicated across listings.

    A run missing any of the three identity fields is not evidence, so it is dropped:
    a dispatch run (dry run, or a promotion itself) does not count as a soak; a run of
    the same tag name from before the tag was moved ran different code.
    """
    out: dict[str, dict] = {}
    for run in runs:
        if run.get("event") != "push":
            continue
        if run.get("head_branch") != tag or run.get("head_sha") != sha:
            continue
        out.setdefault(str(run.get("html_url") or run.get("id")), run)
    return list(out.values())


def judge_soak(
    runs: list[dict],
    *,
    tag: str,
    sha: str,
    hook_results: Sequence[Mapping[str, Any]] | None = None,
    required_staging_gates: Mapping[str, str] | None = None,
) -> Verdict:
    """Pure decision over the runs both listings returned.

    EMPTY is the only state a caller may retry. A pass needs at least one ``success``
    and no concluded run that is not ``success`` (``failure``, ``cancelled``,
    ``timed_out`` ...): a tag whose staging reconcile ever failed on this commit is not
    promoted, wherever that run sits in the list. Runs still in progress are neither.
    """
    relevant = _relevant(runs, tag, sha)
    seen = tuple(
        {
            "conclusion": r.get("conclusion"),
            "status": r.get("status"),
            "url": r.get("html_url"),
        }
        for r in relevant
    )
    if not relevant:
        return Verdict(EMPTY)
    concluded_bad = [
        r for r in relevant if r.get("conclusion") not in (None, "success")
    ]
    green = [r for r in relevant if r.get("conclusion") == "success"]
    if green and not concluded_bad:
        newest = max(green, key=lambda r: str(r.get("created_at") or ""))
        run_url = str(newest.get("html_url") or "")
        created_at = str(newest.get("created_at") or "")
        if required_staging_gates:
            gate_ok, gate_detail = judge_staging_gate(
                list(hook_results or ()), dict(required_staging_gates)
            )
            if not gate_ok:
                return Verdict(
                    STAGING_GATE_FAILED,
                    detail=gate_detail,
                    url=run_url,
                    created_at=created_at,
                    seen=seen,
                )
        return Verdict(
            GREEN,
            url=run_url,
            created_at=created_at,
            seen=seen,
        )
    return Verdict(NOT_GREEN, seen=seen)


def look_up_soak(
    tag: str,
    client: SoakClient,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
    hook_results: Sequence[Mapping[str, Any]] | None = None,
    required_staging_gates: Mapping[str, str] | None = None,
) -> Verdict:
    """Resolve the tag's commit, then look for its soak, retrying only an EMPTY answer.

    Any run that exists is decisive on the attempt that sees it. An attempt that saw no
    run, because both listings were empty or because a listing failed, waits and tries
    again, up to ``attempts``. The final answer is EMPTY or ERROR, never a pass.
    """
    try:
        sha = client.commit_of(tag)
    except LookupFailed as exc:
        return Verdict(ERROR, detail=str(exc))
    sources = ((client.runs_by_commit, sha), (client.runs_by_branch, tag))
    verdict = Verdict(EMPTY, sha=sha)
    for attempt in range(1, attempts + 1):
        runs: list[dict] = []
        errors: list[str] = []
        for source, key in sources:
            try:
                runs.extend(source(key))
            except LookupFailed as exc:
                errors.append(str(exc))
        verdict = replace(
            judge_soak(
                runs,
                tag=tag,
                sha=sha,
                hook_results=hook_results,
                required_staging_gates=required_staging_gates,
            ),
            sha=sha,
        )
        if verdict.state != EMPTY:
            return verdict
        if errors:
            verdict = Verdict(ERROR, detail="; ".join(errors), sha=sha)
        if attempt < attempts:
            note = f" (listing error: {verdict.detail})" if errors else ""
            log(
                f"promotion guard: no run of '{tag}' (commit {sha[:12]}) on attempt "
                f"{attempt}/{attempts}{note}; retrying in {delay_seconds:g}s"
            )
            sleep(delay_seconds)
    return verdict


def refusal_message(
    tag: str, verdict: Verdict, *, attempts: int, delay_seconds: float
) -> str:
    base = (
        f"'{tag}' has no green staging soak: no push-triggered run of this workflow for "
        f"the tag concluded success. Seen: {json.dumps(list(verdict.seen))}. "
        "Fix the release and cut a new tag; production is never promoted from a tag "
        "whose staging reconcile did not pass."
    )
    where = f" (commit {verdict.sha[:12]})" if verdict.sha else ""
    if verdict.state == STAGING_GATE_FAILED:
        return f"{base} Staging gate verification failed{where}: {verdict.detail}."
    if verdict.state == EMPTY:
        return (
            f"{base} The run listing was empty on all {attempts} attempts, "
            f"{delay_seconds:g}s apart, looked up by commit and by branch{where}; if the "
            "soak run exists, wait a minute for GitHub's index and dispatch again."
        )
    if verdict.state == ERROR:
        return (
            f"{base} The run lookup failed on all {attempts} attempts{where}: "
            f"{verdict.detail}."
        )
    return base


class GhClient:
    """Thin ``gh``/``git`` adapter; every failure is a LookupFailed, nothing is swallowed."""

    def __init__(
        self,
        repo: str,
        workflow: str = SOAK_WORKFLOW,
        *,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self.repo = repo
        self.workflow = workflow
        self._run = run

    def _exec(self, argv: list[str]) -> str:
        try:
            proc = self._run(
                argv,
                capture_output=True,
                text=True,
                timeout=GH_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LookupFailed(f"{' '.join(argv[:3])}: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:300]
            raise LookupFailed(
                f"{' '.join(argv[:3])} exited {proc.returncode}: {detail}"
            )
        return proc.stdout

    def commit_of(self, tag: str) -> str:
        sha = self._exec(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}"]
        ).strip()
        if not sha:
            raise LookupFailed(f"git rev-parse: tag '{tag}' resolves to no commit")
        return sha

    def _runs(self, **params: str) -> list[dict]:
        query = urlencode({"event": "push", "per_page": PER_PAGE, **params})
        path = f"repos/{self.repo}/actions/workflows/{self.workflow}/runs?{query}"
        out = self._exec(["gh", "api", path])
        try:
            runs = json.loads(out)["workflow_runs"]
        except (ValueError, KeyError, TypeError) as exc:
            raise LookupFailed(f"gh api {path}: unreadable response ({exc})") from exc
        if not isinstance(runs, list) or not all(isinstance(r, dict) for r in runs):
            raise LookupFailed(f"gh api {path}: workflow_runs is not a list of runs")
        return runs

    def runs_by_commit(self, sha: str) -> list[dict]:
        return self._runs(head_sha=sha)

    def runs_by_branch(self, tag: str) -> list[dict]:
        return self._runs(branch=tag)


def _parse_hook_results_json(raw: str) -> list[dict[str, Any]]:
    text = raw.strip()
    if not text:
        return []
    if text.startswith("@"):
        text = Path(text[1:]).read_text(encoding="utf-8")
    parsed = json.loads(text)
    if isinstance(parsed, Mapping) and "hook_results" in parsed:
        parsed = parsed["hook_results"]
    if not isinstance(parsed, list) or not all(
        isinstance(item, Mapping) for item in parsed
    ):
        raise ValueError("hook_results JSON must be a list of objects")
    return [dict(item) for item in parsed]


def main(
    argv: list[str] | None = None,
    *,
    client: SoakClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("tag", help="release tag being promoted (vX.Y.Z)")
    parser.add_argument(
        "--repo",
        default=os.environ.get("GH_REPO") or os.environ.get("GITHUB_REPOSITORY"),
    )
    parser.add_argument("--workflow", default=SOAK_WORKFLOW)
    parser.add_argument("--attempts", type=int, default=DEFAULT_ATTEMPTS)
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS)
    parser.add_argument(
        "--require-staging-gate",
        action="append",
        default=[],
        metavar="SERVICE=VERSION_REF",
        help="require a green staging_gate hook verdict for SERVICE at VERSION_REF (#1493)",
    )
    parser.add_argument(
        "--hook-results-json",
        default="",
        help="JSON string or @path containing hook_results list from staging reconcile (#1493)",
    )
    args = parser.parse_args(argv)

    if args.attempts < 1 or args.delay < 0:
        print("::error::promotion guard: --attempts must be >= 1 and --delay >= 0.")
        return 1
    required_staging_gates: dict[str, str] = {}
    for item in args.require_staging_gate:
        if "=" not in item:
            print(
                f"::error::promotion guard: expected SERVICE=VERSION_REF for --require-staging-gate, got {item!r}."
            )
            return 1
        svc, ver = item.split("=", 1)
        if not svc.strip() or not ver.strip():
            print(
                f"::error::promotion guard: expected SERVICE=VERSION_REF for --require-staging-gate, got {item!r}."
            )
            return 1
        required_staging_gates[svc.strip()] = ver.strip()

    try:
        hook_results = _parse_hook_results_json(args.hook_results_json)
    except (OSError, ValueError) as exc:
        print(f"::error::promotion guard: unreadable --hook-results-json: {exc}.")
        return 1

    if client is None:
        if not args.repo:
            print("::error::promotion guard: no repository (set GH_REPO or --repo).")
            return 1
        client = GhClient(args.repo, args.workflow)

    verdict = look_up_soak(
        args.tag,
        client,
        attempts=args.attempts,
        delay_seconds=args.delay,
        sleep=sleep,
        hook_results=hook_results,
        required_staging_gates=required_staging_gates or None,
    )
    if verdict.state == GREEN:
        print(
            f"staging soak of {args.tag}: {verdict.url} (run created {verdict.created_at})"
        )
        return 0
    print(
        "::error::"
        + refusal_message(
            args.tag, verdict, attempts=args.attempts, delay_seconds=args.delay
        )
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
