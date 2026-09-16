"""Read-only orchestrator sweep: one state per watched item, waiting on known facts.

On 2026-09-16 the workspace orchestrator waited silently for more than ten minutes on a
pull request because a shell loop grepped a merge gate's prose for the outcomes it
expected and never matched "1 unresolved review thread(s)". This module replaces that
kind of waiter. Every watched item is classified into exactly one state:

    WAITING  time alone will move it (checks running, head settling, agent writing)
    DONE     finished well (PR merged, run green, release printed its done line)
    ACTION   someone must act now (ready to merge, thread open, check red, owner needed)
    STALL    no progress past its threshold, or its process died without a verdict
    UNKNOWN  the probe failed or returned a value this module does not recognise

Only WAITING is a reason to keep waiting, and it is decided from an allow-list of GitHub
facts; any value outside the allow-lists is UNKNOWN, never "probably fine". Merge gates
are judged by exit code only (0 ready, 2 owner); their text is captured and discarded.
Exit 1 alone never extends a wait once nothing time-fixable remains, because a gate
returns 1 for pending checks, red checks, open threads, drafts and closed PRs alike.

Probes only read: ``gh`` queries, ``git`` queries, ``os.stat``, ``pgrep`` and the
configured gate commands, which are refused when they carry a mutating flag. Agent
transcripts are stat()ed for liveness and never opened. The CLI is
``python -m tools.harness sweep`` (exit codes in ``EXIT_CODES``).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

WAITING, DONE, ACTION, STALL, UNKNOWN = "WAITING", "DONE", "ACTION", "STALL", "UNKNOWN"
EXIT_OK, EXIT_ACTION, EXIT_PROGRESS, EXIT_STALL, EXIT_UNKNOWN, EXIT_BUDGET = range(6)
EXIT_CODES = {
    EXIT_OK: "nothing needs you (one-shot: all waiting or done; watch: all done)",
    EXIT_ACTION: "an item needs action",
    EXIT_PROGRESS: "an item finished while others still wait (watch only)",
    EXIT_STALL: "an item stalled or its process died without a verdict",
    EXIT_UNKNOWN: "a probe failed, a value was unrecognised, or the input was invalid",
    EXIT_BUDGET: "the watch budget (--max-minutes) ran out",
}

# Minutes without progress before an item is STALL (coordination.md, Orchestrator
# liveness).
DEFAULT_STALL_MINUTES = {
    "pr": 30,
    "workflow": 45,
    "release_log": 25,
    "worktree": 20,
    "agent": 20,
}
# pr_merge_gate's clock policy settles a fresh head this long.
DEFAULT_SETTLE_MINUTES = 12
HEAD_WITHOUT_RUN_MINUTES = 10
COMMAND_TIMEOUT_SECONDS = 120
MAX_PARALLEL_PROBES = 8
MAX_REVIEW_THREADS = 100
HEARTBEAT_LINE_LIMIT = 600

# Allow-lists. A value outside these sets is UNKNOWN.
CHECK_PENDING = frozenset({"pending"})
CHECK_GREEN = frozenset({"pass", "skipping"})
CHECK_RED = frozenset({"fail", "cancel"})
MERGE_STATES_ACTION = {
    "DIRTY": "merge conflict (DIRTY)",
    "BEHIND": "branch behind base (BEHIND)",
}
MERGE_STATES_KNOWN = frozenset(
    {"CLEAN", "BLOCKED", "UNSTABLE", "HAS_HOOKS", "UNKNOWN", "DRAFT"}
) | frozenset(MERGE_STATES_ACTION)
RUN_ACTIVE = frozenset({"queued", "in_progress", "waiting", "pending", "requested"})
GATE_READY, GATE_NOT_YET, GATE_OWNER = 0, 1, 2
# gh exits 1 with this message, before printing any JSON, when no check has registered.
NO_CHECKS_MARKER = "no checks reported"
# gh documents exit 8 for "checks pending"; the JSON on stdout is still complete.
GH_CHECKS_PENDING_RC = 8
# A sweep only reads. A gate command carrying one of these (or an abbreviation argparse
# would expand to one) could merge, request reviews or change settings, and is refused.
MUTATING_GATE_FLAGS = ("--merge", "--request-review", "--admin", "--auto")
EXIT_MARKER = re.compile(r"^exit=(\d+)\s*$")


class ProbeError(RuntimeError):
    """A probe could not establish its facts; the item is reported as UNKNOWN."""


class SweepConfigError(ValueError):
    """The watch list cannot be used."""


# --- plumbing


@dataclass(frozen=True)
class CommandResult:
    rc: int
    out: str
    err: str


Runner = Callable[[Sequence[str], "str | None"], CommandResult]


def run_command(argv: Sequence[str], cwd: str | None = None) -> CommandResult:
    try:
        done = subprocess.run(
            list(argv),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # A missing binary or a hang is a probe failure, reported with the same code a
        # shell uses for "command not found" so a gate can never read it as a verdict.
        return CommandResult(127, "", f"{type(exc).__name__}: {exc}")
    return CommandResult(done.returncode, done.stdout, done.stderr)


def stat_mtime(path: str) -> tuple[float, int] | None:
    try:
        st = os.stat(path)  # follows a tasks/*.output symlink; never opens the file
    except OSError:
        return None
    return st.st_mtime, st.st_size


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class Env:
    """Every side-effecting read goes through here, so tests can replace it."""

    run: Runner = run_command
    now: Callable[[], float] = time.time
    stat: Callable[[str], "tuple[float, int] | None"] = stat_mtime
    alive: Callable[[int], bool] = pid_alive


def gh_json(run: Runner, argv: Sequence[str]) -> Any:
    res = run(["gh", *argv], None)
    if res.rc != 0:
        raise ProbeError(
            f"gh {' '.join(argv[:3])} exit {res.rc}: {res.err.strip()[:160]}"
        )
    return json.loads(res.out or "null")


def epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def minutes(seconds: float) -> str:
    return f"{seconds / 60:.0f}m" if seconds >= 90 else f"{seconds:.0f}s"


@dataclass(frozen=True)
class Status:
    key: str
    state: str
    detail: str
    # What counts as a transition: the state plus these facts. Ages are left out, so a
    # quiet item stays quiet in --watch output.
    fingerprint: tuple = ()

    def line(self) -> str:
        return f"{self.state:<7} {self.key}: {self.detail}"


# --- pull requests


@dataclass(frozen=True)
class PrFacts:
    state: str
    draft: bool
    head: str
    merge_state: str
    review_decision: str
    head_at: float  # newest commit time on the head, the clock both merge gates use
    updated_at: float
    # (name, gh bucket, started epoch, completed epoch)
    checks: tuple[tuple[str, str, float | None, float | None], ...]
    unresolved: int
    threads_total: int


def _read_checks(run: Runner, repo: str, number: int) -> list[dict]:
    res = run(
        [
            "gh",
            "pr",
            "checks",
            str(number),
            "-R",
            repo,
            "--json",
            "name,bucket,startedAt,completedAt",
        ],
        None,
    )
    if res.rc in (0, GH_CHECKS_PENDING_RC) and res.out.strip():
        return list(json.loads(res.out))
    if res.rc == 0:
        return []
    if NO_CHECKS_MARKER in res.err:
        return []
    raise ProbeError(f"gh pr checks exit {res.rc}: {res.err.strip()[:160]}")


def review_threads_query(repo: str, number: int) -> str:
    owner, name = repo.split("/", 1)
    return (
        "query{repository(owner:%s,name:%s){pullRequest(number:%d)"
        "{reviewThreads(first:%d){totalCount nodes{isResolved}}}}}"
        % (json.dumps(owner), json.dumps(name), number, MAX_REVIEW_THREADS)
    )


def collect_pr(env: Env, repo: str, number: int) -> PrFacts:
    view = gh_json(
        env.run,
        [
            "pr",
            "view",
            str(number),
            "-R",
            repo,
            "--json",
            "state,isDraft,headRefOid,mergeStateStatus,reviewDecision,updatedAt,commits",
        ],
    )
    raw = _read_checks(env.run, repo, number)
    threads = gh_json(
        env.run, ["api", "graphql", "-f", f"query={review_threads_query(repo, number)}"]
    )
    node = threads["data"]["repository"]["pullRequest"]["reviewThreads"]
    commits = view.get("commits") or []
    return PrFacts(
        state=str(view.get("state") or ""),
        draft=bool(view.get("isDraft")),
        head=str(view.get("headRefOid") or ""),
        merge_state=str(view.get("mergeStateStatus") or ""),
        review_decision=str(view.get("reviewDecision") or ""),
        head_at=max(
            (epoch(c.get("committedDate")) or 0.0 for c in commits), default=0.0
        ),
        updated_at=epoch(view.get("updatedAt")) or 0.0,
        checks=tuple(
            (
                str(c.get("name")),
                str(c.get("bucket") or ""),
                epoch(c.get("startedAt")),
                epoch(c.get("completedAt")),
            )
            for c in raw
        ),
        unresolved=sum(1 for n in node["nodes"] if not n.get("isResolved")),
        threads_total=int(node.get("totalCount") or 0),
    )


def _blocking_facts(f: PrFacts) -> list[str]:
    """Facts that time will never fix; they win over anything still running."""
    found = []
    red = sorted(name for name, bucket, _, _ in f.checks if bucket in CHECK_RED)
    if red:
        more = " …" if len(red) > 4 else ""
        found.append(f"red check(s): {', '.join(red[:4])}{more}")
    if f.unresolved:
        found.append(f"{f.unresolved} unresolved review thread(s)")
    if f.threads_total > MAX_REVIEW_THREADS:
        found.append(f"{f.threads_total} threads, only {MAX_REVIEW_THREADS} read")
    if f.review_decision == "CHANGES_REQUESTED":
        found.append("changes requested")
    if f.merge_state in MERGE_STATES_ACTION:
        found.append(MERGE_STATES_ACTION[f.merge_state])
    return found


def classify_pr(
    key: str,
    f: PrFacts,
    gate_rc: int | None,
    *,
    now: float,
    settle_minutes: float = DEFAULT_SETTLE_MINUTES,
    stall_minutes: float = DEFAULT_STALL_MINUTES["pr"],
) -> Status:
    head = f.head[:7]
    if f.state == "MERGED":
        return Status(key, DONE, f"merged @ {head}", (f.state, f.head))
    if f.state != "OPEN":
        state = f.state or "unknown state"
        return Status(key, ACTION, f"{state} without merge @ {head}", (f.state, f.head))

    buckets = {bucket for _, bucket, _, _ in f.checks}
    strange = sorted(buckets - CHECK_PENDING - CHECK_GREEN - CHECK_RED)
    if strange or f.merge_state not in MERGE_STATES_KNOWN:
        what = (
            f"check bucket(s) {strange}"
            if strange
            else f"mergeStateStatus {f.merge_state!r}"
        )
        return Status(key, UNKNOWN, f"unrecognised {what} @ {head}", (f.head, what))

    # 1. Facts time will never fix.
    blocking = _blocking_facts(f)
    if blocking:
        return Status(
            key,
            ACTION,
            f"{'; '.join(blocking)} @ {head}",
            (f.head, tuple(sorted(blocking))),
        )

    # 2. The gate's exit code decides ready / owner. Its text is never read.
    if gate_rc == GATE_READY:
        return Status(key, ACTION, f"READY: gate exit 0 @ {head}", (f.head, "ready"))
    if gate_rc == GATE_OWNER:
        return Status(
            key,
            ACTION,
            f"OWNER: gate exit 2 (owner approval of {head} required)",
            (f.head, "owner"),
        )
    if gate_rc not in (None, GATE_NOT_YET):
        return Status(
            key,
            UNKNOWN,
            f"gate exit {gate_rc} @ {head} (not a verdict)",
            (f.head, gate_rc),
        )

    # 3. The allow-list: the only reasons to keep waiting.
    waiting = _waiting_reason(key, f, now, settle_minutes, stall_minutes)
    if waiting is not None:
        return waiting

    # 4. Nothing time-fixable remains. A gate still saying "not yet" needs a reader.
    if gate_rc == GATE_NOT_YET:
        return Status(
            key,
            ACTION,
            "gate exit 1 with no pending check and the settle window over "
            f"({f.merge_state}) — read the gate output @ {head}",
            (f.head, "gate-not-yet"),
        )
    if f.merge_state == "CLEAN":
        return Status(
            key,
            ACTION,
            f"mergeable by facts (no gate configured) @ {head}",
            (f.head, "clean"),
        )
    return Status(
        key,
        ACTION,
        f"mergeStateStatus {f.merge_state} with checks green, threads resolved @ {head}",
        (f.head, f.merge_state),
    )


def _waiting_reason(
    key: str, f: PrFacts, now: float, settle_minutes: float, stall_minutes: float
) -> Status | None:
    head = f.head[:7]
    stall = stall_minutes * 60
    head_age = now - f.head_at
    if f.draft:
        idle = now - f.updated_at
        if idle > stall:
            return Status(
                key,
                STALL,
                f"draft, no activity for {minutes(idle)} @ {head}",
                (f.head, "draft-stall"),
            )
        return Status(
            key,
            WAITING,
            f"draft, author active {minutes(idle)} ago @ {head}",
            (f.head, "draft"),
        )
    pending = sorted(name for name, bucket, _, _ in f.checks if bucket in CHECK_PENDING)
    if pending:
        finished = [
            done
            for _, bucket, _, done in f.checks
            if bucket not in CHECK_PENDING and done
        ]
        quiet = now - max([f.head_at, *finished])
        names = ", ".join(pending[:3])
        if quiet > stall:
            return Status(
                key,
                STALL,
                f"{len(pending)} check(s) pending, none finished for "
                f"{minutes(quiet)}: {names} @ {head}",
                (f.head, "checks-stall"),
            )
        return Status(
            key,
            WAITING,
            f"{len(pending)} check(s) pending ({names}) @ {head}",
            (f.head, "checks"),
        )
    if not f.checks:
        if head_age > stall:
            return Status(
                key,
                STALL,
                f"no checks registered {minutes(head_age)} after push @ {head}",
                (f.head, "nochecks-stall"),
            )
        return Status(
            key,
            WAITING,
            f"no checks registered yet, head {minutes(head_age)} old @ {head}",
            (f.head, "nochecks"),
        )
    if f.merge_state == "UNKNOWN" and head_age < stall:
        return Status(
            key,
            WAITING,
            f"GitHub still computing mergeability @ {head}",
            (f.head, "computing"),
        )
    if head_age < settle_minutes * 60:
        return Status(
            key,
            WAITING,
            f"settling: head {minutes(head_age)} old of {settle_minutes:g}m @ {head}",
            (f.head, "settling"),
        )
    return None


def mutating_flags(argv: Sequence[str]) -> list[str]:
    """Flags in argv that are, or that argparse would expand to, a mutating flag.

    Every token is scanned, including those after a ``--``: a gate argv is often a
    wrapper chain (``uv run -- python -m tools.pr_merge_gate N --merge``) where ``--``
    ends the wrapper's options and the rest reaches the gate as options. A literal
    positional ``--merge`` is refused too; a refusal is a visible UNKNOWN, never a merge.
    """
    found = []
    for token in argv:
        name = str(token).split("=", 1)[0]
        if len(name) <= 2 or not name.startswith("--"):
            continue  # "--" itself and short flags never expand to these
        found.extend(flag for flag in MUTATING_GATE_FLAGS if flag.startswith(name))
    return sorted(set(found))


def run_gate(env: Env, gate: dict, number: int) -> int:
    argv = [str(a).replace("{number}", str(number)) for a in gate["argv"]]
    forbidden = mutating_flags(argv)
    if forbidden:
        raise ProbeError(
            f"gate argv carries mutating flag(s) {forbidden}; the sweep is read-only"
        )
    return env.run(argv, gate.get("cwd")).rc


def resolve_pr_number(env: Env, repo: str, head: str) -> int | None:
    rows = gh_json(
        env.run,
        [
            "pr",
            "list",
            "-R",
            repo,
            "--head",
            head,
            "--state",
            "all",
            "--json",
            "number,state,updatedAt",
            "--limit",
            "10",
        ],
    )
    if not rows:
        return None
    rows.sort(
        key=lambda r: (r.get("state") == "OPEN", r.get("updatedAt") or ""),
        reverse=True,
    )
    return int(rows[0]["number"])


def probe_pr(env: Env, item: dict, key: str, memory: dict) -> Status:
    repo = item["repo"]
    number = item.get("number")
    stall = float(item.get("stall_minutes", DEFAULT_STALL_MINUTES["pr"]))
    if number is None:
        number = resolve_pr_number(env, repo, item["head"])
        if number is None:
            # No PR yet: the only clock is when this watch first saw the gap.
            first = memory.setdefault(("first_seen", key), env.now())
            waited = env.now() - first
            if waited > stall * 60:
                return Status(
                    key,
                    STALL,
                    f"no PR for head {item['head']} after {minutes(waited)}",
                    ("nopr-stall",),
                )
            return Status(key, WAITING, f"no PR for head {item['head']} yet", ("nopr",))
    facts = collect_pr(env, repo, int(number))
    gate_rc = None
    if item.get("gate") and facts.state == "OPEN":
        gate_rc = run_gate(env, item["gate"], int(number))
    status = classify_pr(
        key,
        facts,
        gate_rc,
        now=env.now(),
        settle_minutes=float(item.get("settle_minutes", DEFAULT_SETTLE_MINUTES)),
        stall_minutes=stall,
    )
    if item.get("number") is None:  # resolved from a head: say which PR it is
        status = Status(
            key, status.state, f"#{number} {status.detail}", status.fingerprint
        )
    return status


# --- workflow runs


def classify_run(
    key: str,
    run: dict | None,
    *,
    now: float,
    stall_minutes: float,
    branch_head: str | None = None,
    branch_head_at: float | None = None,
) -> Status:
    if branch_head and (run is None or run.get("headSha") != branch_head):
        age = now - (branch_head_at or now)
        if age > HEAD_WITHOUT_RUN_MINUTES * 60:
            return Status(
                key,
                STALL,
                f"head {branch_head[:7]} has no run {minutes(age)} after its commit",
                (branch_head, "norun-stall"),
            )
        return Status(
            key,
            WAITING,
            f"head {branch_head[:7]} has no run yet",
            (branch_head, "norun"),
        )
    if run is None:
        return Status(key, UNKNOWN, "no runs found", ("none",))
    rid = run.get("databaseId")
    status = str(run.get("status") or "")
    conclusion = str(run.get("conclusion") or "")
    sha = str(run.get("headSha") or "")[:7]
    fp = (rid, status, conclusion)
    if status in RUN_ACTIVE:
        age = now - (epoch(run.get("createdAt")) or now)
        if age > stall_minutes * 60:
            return Status(
                key, STALL, f"run {rid} {status} for {minutes(age)} @ {sha}", fp
            )
        return Status(key, WAITING, f"run {rid} {status} ({minutes(age)}) @ {sha}", fp)
    if status == "completed":
        if conclusion == "success":
            return Status(key, DONE, f"run {rid} success @ {sha}", fp)
        return Status(
            key, ACTION, f"run {rid} {conclusion or 'no conclusion'} @ {sha}", fp
        )
    return Status(key, UNKNOWN, f"run {rid} status {status!r}", fp)


def probe_workflow(env: Env, item: dict, key: str, memory: dict) -> Status:
    """The newest run of a workflow; with ``expect_branch_head``, the run of the
    branch's current head commit, so an older or re-run commit cannot stand in for it.
    Use ``expect_branch_head`` only for workflows that run on every push to the branch:
    a path-filtered workflow legitimately has no run for some heads."""
    repo, workflow = item["repo"], item["workflow"]
    argv = [
        "run",
        "list",
        "-R",
        repo,
        "--workflow",
        workflow,
        "--limit",
        "1",
        "--json",
        "databaseId,status,conclusion,headSha,createdAt",
    ]
    if item.get("branch"):
        argv += ["--branch", item["branch"]]
    if item.get("event"):
        argv += ["--event", item["event"]]
    head = head_at = None
    if item.get("expect_branch_head") and item.get("branch"):
        commit = gh_json(env.run, ["api", f"repos/{repo}/commits/{item['branch']}"])
        head = str(commit["sha"])
        head_at = epoch(commit["commit"]["committer"]["date"])
        argv += ["--commit", head]
    rows = gh_json(env.run, argv)
    return classify_run(
        key,
        rows[0] if rows else None,
        now=env.now(),
        stall_minutes=float(
            item.get("stall_minutes", DEFAULT_STALL_MINUTES["workflow"])
        ),
        branch_head=head,
        branch_head_at=head_at,
    )


# --- release logs


def classify_release_log(
    key: str,
    text: str | None,
    mtime: float | None,
    *,
    now: float,
    stall_minutes: float,
    alive: bool | None,
    done_lines: Iterable[str] = ("== done ==",),
    fail_prefixes: Iterable[str] = ("cut_release:",),
) -> Status:
    if text is None or mtime is None:
        return Status(key, UNKNOWN, "log missing", ("missing",))
    fail_prefixes = tuple(fail_prefixes)
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    stage = next((ln for ln in reversed(lines) if ln.startswith("== ")), "no stage yet")
    # The appended verdict line (`cmd > log 2>&1; echo "exit=$?" >> log`) decides first.
    for ln in reversed(lines):
        marker = EXIT_MARKER.match(ln)
        if marker:
            rc = int(marker.group(1))
            if rc == 0:
                return Status(key, DONE, f"exit 0 after {stage}", ("exit", 0))
            failure = next(
                (x for x in reversed(lines) if x.startswith(fail_prefixes)), stage
            )
            return Status(key, ACTION, f"exit {rc}: {failure[:160]}", ("exit", rc))
    failure = next((x for x in lines if x.startswith(fail_prefixes)), None)
    if failure:
        return Status(
            key, ACTION, f"failed at {stage}: {failure[:160]}", ("failed", failure)
        )
    if lines and lines[-1].strip() in set(done_lines):
        promote = any("promote with" in ln for ln in lines)
        tail = "; a promote step is printed" if promote else ""
        prev = next((ln for ln in reversed(lines[:-1]) if ln.startswith("== ")), stage)
        return Status(key, DONE, f"done after {prev}{tail}", ("done",))
    quiet = now - mtime
    if alive is False:
        return Status(
            key,
            STALL,
            f"DIED: process gone, no verdict line; last stage {stage}",
            ("died", stage),
        )
    if quiet > stall_minutes * 60:
        return Status(
            key,
            STALL,
            f"no log write for {minutes(quiet)}; last stage {stage}",
            ("quiet", stage),
        )
    running = ", process alive" if alive else ""
    return Status(
        key,
        WAITING,
        f"{stage} (last write {minutes(quiet)} ago{running})",
        ("running", stage),
    )


def probe_release_log(env: Env, item: dict, key: str, memory: dict) -> Status:
    path = item["path"]
    st = env.stat(path)
    text = None
    if st is not None:
        # The orchestrator's own background log, not an agent transcript.
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    alive = None
    if item.get("pid"):
        alive = env.alive(int(item["pid"]))
    elif item.get("process_match"):
        alive = env.run(["pgrep", "-f", item["process_match"]], None).rc == 0
    return classify_release_log(
        key,
        text,
        st[0] if st else None,
        now=env.now(),
        stall_minutes=float(
            item.get("stall_minutes", DEFAULT_STALL_MINUTES["release_log"])
        ),
        alive=alive,
        done_lines=item.get("done_lines", ("== done ==",)),
        fail_prefixes=item.get("fail_prefixes", ("cut_release:",)),
    )


# --- agents and worktrees


def probe_agent(env: Env, item: dict, key: str, memory: dict) -> Status:
    st = env.stat(item["output"])  # liveness only: the transcript is never opened
    if st is None:
        return Status(key, UNKNOWN, "output file missing", ("missing",))
    quiet = env.now() - st[0]
    stall = float(item.get("stall_minutes", DEFAULT_STALL_MINUTES["agent"]))
    if quiet > stall * 60:
        return Status(
            key,
            STALL,
            f"no transcript write for {minutes(quiet)} — SendMessage for status",
            ("quiet",),
        )
    return Status(
        key,
        WAITING,
        f"active, last write {minutes(quiet)} ago ({st[1] // 1024} KiB)",
        ("active",),
    )


def _porcelain_paths(status_out: str) -> list[str]:
    paths = []
    for line in status_out.splitlines():
        if line.strip():
            paths.append(line[3:].split(" -> ")[-1].strip('"'))
    return paths


def probe_worktree(env: Env, item: dict, key: str, memory: dict) -> Status:
    path = item["path"]
    if not Path(path).is_dir():
        return Status(
            key,
            ACTION,
            "worktree gone — drop it from the watch list or find where the work went",
            ("gone",),
        )

    def git(*args: str) -> CommandResult:
        return env.run(["git", "-C", path, *args], None)

    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    log = git("log", "-1", "--format=%H %ct")
    status = git("status", "--porcelain=v1")
    if branch.rc or log.rc or status.rc:
        err = (branch.err or log.err or status.err).strip()[:120]
        return Status(key, UNKNOWN, f"git failed: {err}", ("git",))
    head, committed = log.out.split()
    dirty = _porcelain_paths(status.out)
    stats = (env.stat(os.path.join(path, p)) for p in dirty)
    activity = max([float(committed), *(s[0] for s in stats if s)])
    ahead = git("rev-list", "--count", "@{u}..HEAD")
    unpushed = ahead.out.strip() if ahead.rc == 0 else "no upstream"
    quiet = env.now() - activity
    stall = float(item.get("stall_minutes", DEFAULT_STALL_MINUTES["worktree"]))
    detail = (
        f"{branch.out.strip()} @ {head[:7]}, dirty={len(dirty)}, "
        f"unpushed={unpushed}, last activity {minutes(quiet)} ago"
    )
    fingerprint = (head, unpushed)
    if quiet > stall * 60:
        return Status(key, STALL, detail + " — SendMessage for status", fingerprint)
    return Status(key, WAITING, detail, fingerprint)


Probe = Callable[[Env, dict, str, dict], Status]
PROBES: dict[str, Probe] = {
    "pr": probe_pr,
    "workflow": probe_workflow,
    "release_log": probe_release_log,
    "agent": probe_agent,
    "worktree": probe_worktree,
}


def item_key(item: dict) -> str:
    """One stable key per item, the same whether its probe succeeds or fails."""
    if item.get("label"):
        return str(item["label"])
    kind = item.get("kind")
    repo = str(item.get("repo") or "").split("/")[-1]
    if kind == "pr":
        ref = item.get("number") or item.get("head")
        return f"{repo}#{ref}" if item.get("number") else f"{repo}@{ref}"
    if kind == "workflow":
        branch = f"@{item['branch']}" if item.get("branch") else ""
        return f"{repo}:{item.get('workflow')}{branch}"
    if kind == "release_log":
        return f"release:{Path(str(item.get('path'))).name}"
    if kind == "agent":
        return f"agent:{item.get('name')}"
    if kind == "worktree":
        return f"worktree:{Path(str(item.get('path'))).name}"
    return json.dumps(item, sort_keys=True)[:80]


# --- sweep and watch


def probe_one(env: Env, item: dict, memory: dict) -> Status:
    key = item_key(item)
    probe = PROBES.get(str(item.get("kind", "")))
    if probe is None:
        return Status(key, UNKNOWN, f"unknown kind {item.get('kind')!r}", ("kind",))
    try:
        return probe(env, item, key, memory)
    except Exception as exc:  # noqa: BLE001 - a failed probe is a visible UNKNOWN
        return Status(key, UNKNOWN, f"probe error: {exc}"[:200], ("error",))


def sweep(env: Env, items: Sequence[dict], memory: dict) -> list[Status]:
    """Probe every item in parallel (independent reads), keeping watch-list order."""
    workers = min(MAX_PARALLEL_PROBES, len(items) or 1)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda item: probe_one(env, item, memory), items))


def exit_code(statuses: Sequence[Status], *, watching: bool) -> int:
    states = {s.state for s in statuses}
    if UNKNOWN in states:
        return EXIT_UNKNOWN
    if STALL in states:
        return EXIT_STALL
    if ACTION in states:
        return EXIT_ACTION
    if watching and DONE in states and WAITING in states:
        return EXIT_PROGRESS
    return EXIT_OK


def stamp(now: float) -> str:
    return datetime.fromtimestamp(now).strftime("%H:%M:%S")


def sweep_once(env: Env, items: Sequence[dict], emit: Callable[[str], None]) -> int:
    statuses = sweep(env, items, {})
    for status in statuses:
        emit(status.line())
    code = exit_code(statuses, watching=False)
    emit(f"sweep: exit {code} ({EXIT_CODES[code]})")
    return code


def watch(
    env: Env,
    items: Sequence[dict],
    *,
    interval: float,
    heartbeat: float,
    max_minutes: float,
    unknown_tolerance: int,
    emit: Callable[[str], None],
    sleep: Callable[[float], None],
) -> int:
    """Print transitions and heartbeats; return as soon as any item leaves WAITING."""
    memory: dict = {}
    previous: dict[str, Status] = {}
    started = last_emit = env.now()
    unknown_streak = 0
    while True:
        statuses = sweep(env, items, memory)
        now = env.now()
        changed = False
        for status in statuses:
            before = previous.get(status.key)
            if before is None or (before.state, before.fingerprint) != (
                status.state,
                status.fingerprint,
            ):
                arrow = "" if before is None else f"{before.state}->"
                emit(f"{stamp(now)} {arrow}{status.line()}")
                changed = True
        previous = {s.key: s for s in statuses}
        unknown = sum(1 for s in statuses if s.state == UNKNOWN)
        unknown_streak = unknown_streak + 1 if unknown else 0
        leaving = [s for s in statuses if s.state not in (WAITING, UNKNOWN)]
        if leaving or unknown_streak > unknown_tolerance:
            code = exit_code(statuses, watching=True)
            waiting = sum(1 for s in statuses if s.state == WAITING)
            emit(
                f"{stamp(now)} sweep: exit {code} ({EXIT_CODES[code]}) — "
                f"{waiting} waiting, {len(statuses) - waiting} need a look"
            )
            return code
        if changed:
            last_emit = now
        elif now - last_emit >= heartbeat:
            summary = " | ".join(f"{s.key}: {s.detail[:50]}" for s in statuses)
            line = (
                f"{stamp(now)} heartbeat: {len(statuses) - unknown} waiting, "
                f"{unknown} unknown, unchanged — {summary}"
            )
            emit(line[:HEARTBEAT_LINE_LIMIT])
            last_emit = now
        if max_minutes and now - started >= max_minutes * 60:
            emit(
                f"{stamp(now)} sweep: exit {EXIT_BUDGET} "
                f"({EXIT_CODES[EXIT_BUDGET]}) — {max_minutes:g}m spent, re-arm"
            )
            return EXIT_BUDGET
        sleep(interval)


def load_items(path: Path) -> list[dict]:
    """Read ``{"items": [...]}`` or a bare list; an empty or broken list is an error."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SweepConfigError(f"cannot read watch list {path}: {exc}") from exc
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list) or not items:
        raise SweepConfigError(f"watch list {path} lists no items")
    bad = [i for i, item in enumerate(items) if not isinstance(item, dict)]
    if bad:
        raise SweepConfigError(f"watch list {path}: item(s) {bad} are not objects")
    return items
