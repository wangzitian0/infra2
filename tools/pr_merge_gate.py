#!/usr/bin/env python3
"""The session-scoped merge authority of AGENTS.md, as a check instead of a habit.

An agent merging under session authority must see, for one head, that every blocking
check is green, every review thread is resolved, the head has been quiet for twelve
minutes since its last push, and that the change neither touches a protected file nor
triggers a deploy on merge — the last two need the owner's approval of that exact head.
On 2026-09-15 three of five merges landed 21–95 s before the quiet period had elapsed,
each time because the timing was judged by eye between other work. This tool judges it.

    python -m tools.pr_merge_gate 704            # verdict, exit 0 when mergeable by rule
    python -m tools.pr_merge_gate 704 --merge    # squash-merge only when the verdict is ready

Exit codes: 0 ready (or merged), 1 not yet (a check pending, a thread open, the head
still settling), 2 needs the owner (protected file, deploy-triggering path, wrong base).

Two policies for the settling condition:

- ``clock`` (default, AGENTS.md as written): twelve minutes since the last push.
- ``event``: an automated review has been submitted on the *current* head and three
  minutes have passed since that review — the thing the clock was waiting for, measured.
  On 2026-09-15 Copilot reviewed each first push within 2–3 minutes and never re-reviewed
  a fix-up push on its own, so the clock waited on nothing; ``--request-review`` asks
  Copilot for a review of the head when none exists.
- ``either``: whichever of the two is satisfied first — the review lets a head go early,
  the clock stays the upper bound when no automated review ever arrives.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import functools
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

import yaml
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent
# A field `gh` was asked for and did not return, distinct from one never asked for.
ABSENT = "ABSENT"
DEFAULT_REPO = "wangzitian0/infra2"
QUIET_MINUTES = 12
SETTLE_MINUTES = 3  # event policy: after the review of the head, not after the push
# GitHub's Copilot pull-request reviewer (a global bot id, the same in every repository).
COPILOT_BOT_ID = "BOT_kgDOCnlnWA"
AUTOMATED_REVIEWERS = frozenset({"copilot-pull-request-reviewer"})

# The gate decides from the working tree, so an agent merging its own pull
# request is judged by the version that pull request introduces. What "judges"
# means is not a matter of taste -- it is this module plus everything it reads
# to reach a verdict, which is a transitive closure and therefore computable.
#
# It used to be a hand-written list of five paths, and a hand-written list is
# open: it missed `tools/omca_gate_policy.py` (the blocking audit layer),
# `libs/console.py`, and omca's test, all of which this module imports. Adding
# an import used to silently widen what judges a pull request without widening
# what protects it. Computing the closure closes that by construction.
#
# Two files are listed rather than computed, for the one reason a closure over
# code cannot reach them: no code reads them. They are the rules themselves --
# `AGENTS.md` holds the merge-authority invariants, `ops.merge-gate.md` the
# clauses. Both appear in this module only inside comments, which the closure
# deliberately does not follow: following prose would sweep in every path ever
# mentioned in a docstring.
#
# This is exactly where a computed set can quietly lose ground a listed one
# held, so `test_the_closure_still_covers_everything_the_list_did` pins it.
RULE_TEXT_FILES = ("AGENTS.md", "docs/ssot/ops.merge-gate.md")

# AGENTS.md §6's last line: a change to RULE_TEXT_FILES may skip the owner escalation
# that `evaluate` would otherwise apply -- not because the prose stopped mattering, but
# because a direction proof is impossible for prose (see the DIRECTION_PROOFS comment
# below) and a cited instruction is the substitute the owner accepted for these two
# files specifically. It is not a substitute for self_governing_files() as a whole:
# pr_merge_gate.py itself, what it imports, the data it reads, and their tests are the
# defendant-rewrites-the-law case and stay unproven regardless of what the body says.
#
# The header must be followed by an actual quote, not just a claim that one exists --
# a heading with nothing under it, or prose that merely mentions "owner instruction",
# proves nothing a reviewer could not equally assert for any other edit.
# Kept byte-for-byte identical to the regex the merge-gate ruling (#814) specifies,
# so the code and the SSOT that documents it cannot silently drift apart.
_OWNER_INSTRUCTION_HEADER_RE = re.compile(
    r"(?im)^(##+\s*)?(owner instruction|owner 指示)\b.*$"
)
# A quote must actually quote something: a bare `>` or an empty `「」` is a
# citation of nothing and must not pass. Two shapes count, per the SSOT wording
# ("`>` 开头，或含「...」原话"): (a) a `>` blockquote *with content after the
# marker* -- anchored, since that is markdown quote syntax and only means
# something at the start of the line; (b) a line that merely *contains* a
# non-empty 「...」 quote anywhere -- unanchored, since "含" (contains) does not
# require the quote to open the line. `.search()`, not `.match()`, evaluates
# this: `^` inside alternative (a) still pins it to line-start, while
# alternative (b) is free to match further in.
_QUOTE_LINE_RE = re.compile(r"^>\s*\S|「[^」]*\S[^」]*」")


def _owner_instruction_quoted(body: str) -> bool:
    """True when the PR body cites the owner instruction, not just names it.

    Looks for a line matching `_OWNER_INSTRUCTION_HEADER_RE`, then the first
    non-blank line after it: a `>` blockquote with content after the marker, or
    a line containing a non-empty 「...」original-words quote, counts -- a bare
    `>` or an empty 「」does not. Multiple headers are tried independently, so
    one empty attempt does not shadow a real citation further down the body.
    """
    lines = (body or "").splitlines()
    for i, line in enumerate(lines):
        if not _OWNER_INSTRUCTION_HEADER_RE.match(line):
            continue
        for later in lines[i + 1 :]:
            stripped = later.strip()
            if not stripped:
                continue
            if _QUOTE_LINE_RE.search(stripped):
                return True
            break  # first non-blank line under this header is not a quote
    return False


# AGENTS.md point 3: unresolved findings are weighted high=1.0 / middle=0.5 /
# low=0.25, unlabelled counts as middle, and a total of 1.0 or more blocks.
# The rule was written down and never implemented -- the gate counted threads,
# so one unlabelled finding of any kind blocked a merge. Two consequences, and
# the second is why this is worth implementing rather than deleting: a single
# `high` weighed the same as a single nit, and a reviewer that never labels
# severity (no automated one does) could stall a pull request on a wording
# preference.
SEVERITY_WEIGHTS = {"high": 1.0, "middle": 0.5, "medium": 0.5, "low": 0.25}
UNLABELLED_SEVERITY_WEIGHT = SEVERITY_WEIGHTS["middle"]
BLOCKING_SEVERITY_TOTAL = 1.0
# Only an explicit label counts. Inferring severity from prose would make the
# verdict depend on how a sentence is phrased, which is the opposite of what a
# gate is for.
_SEVERITY_RE = re.compile(
    r"\bseverity\s*[:：]\s*\**(high|middle|medium|low)\b", re.IGNORECASE
)


def thread_weight(bodies: Sequence[str]) -> float:
    """The weight of one unresolved thread.

    The highest label anywhere in the thread wins: a reply that downgrades its
    own nit does not lower a `high` raised above it, and a thread resolves as a
    whole or not at all.
    """
    best = 0.0
    for body in bodies:
        for match in _SEVERITY_RE.finditer(body or ""):
            best = max(best, SEVERITY_WEIGHTS[match.group(1).lower()])
    return best or UNLABELLED_SEVERITY_WEIGHT


# Repository-internal data a gate module may read. Kept narrow on purpose --
# a wider pattern would sweep in every path mentioned in prose.
_DATA_SUFFIXES = (".json", ".yaml", ".yml", ".md")


def _repo_deps(rel: str) -> set[str]:
    """Repository files this module imports or reads, one level deep."""
    found: set[str] = set()
    path = ROOT / rel
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return found
    try:
        tree = ast.parse(source)
    except SyntaxError:
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                stem = name.replace(".", "/")
                for candidate in (f"{stem}.py", f"{stem}/__init__.py"):
                    if (ROOT / candidate).is_file():
                        found.add(candidate)
    for match in re.finditer(r"""["']((?:docs|libs|tools)/[\w./-]+)["']""", source):
        rel_data = match.group(1)
        if rel_data.endswith(_DATA_SUFFIXES) and (ROOT / rel_data).is_file():
            found.add(rel_data)
    return found


@functools.lru_cache(maxsize=1)
def self_governing_files() -> frozenset[str]:
    """Everything a change to which would let this gate judge its own rewrite.

    The closure from this module, plus each closure member's own test (a test
    ships in the same pull request and can be edited by it), plus the rule
    texts no code reads.

    Falls back to the closure's seed if the walk finds nothing, so a parsing
    failure cannot quietly empty the protected set -- an empty set here would
    let any rewrite through, which is the one outcome worse than a wrong one.
    """
    seed = "tools/pr_merge_gate.py"
    closure = {seed}
    frontier = {seed}
    while frontier:
        nxt: set[str] = set()
        for member in frontier:
            if member.endswith(".py"):
                nxt |= _repo_deps(member)
        nxt -= closure
        closure |= nxt
        frontier = nxt
    for member in list(closure):
        if member.endswith(".py"):
            test = f"libs/tests/test_{PurePosixPath(member).stem}.py"
            if (ROOT / test).is_file():
                closure.add(test)
    closure |= set(RULE_TEXT_FILES)
    # 每一个 workflow 文件。它们**决定这个门禁怎么判**，而闭包走不到它们：
    # `WORKFLOW_DIR = ROOT / ".github" / "workflows"` 是路径拼接，`_repo_deps` 的
    # 字面量正则看不见；而 `_declared_deploy_globs()` 每次都从工作树重读它们来决定
    # 哪些路径一合就部署，`_required_checks()` 也从它们解析必需检查的显示名。
    #
    # 审计实测（2026-09-22）的绕过：一个 PR 同时改 `libs/alerting.py` 和
    # `apply-observability.yml`，把前者从后者的 `on.push.paths` 里删掉 ——
    # 门禁读的是这个 PR 自己那份 workflow，于是「合流会触发 observability apply」
    # 这条升级不再成立，判定为 ready=True / owner_required=False / exit=0。
    # 那是 AGENTS.md 明文列为必须回 owner 的一类，被会话权限直接合掉。
    closure |= {f for f in _all_workflow_files() if (ROOT / f).is_file()}
    if seed not in closure:  # pragma: no cover - defensive
        closure = {seed, *RULE_TEXT_FILES}
    return frozenset(closure)


def _all_workflow_files() -> list[str]:
    """`.github/workflows` 下的每个 workflow，相对仓库根。"""
    # 直接拼相对路径，不用 `relative_to(ROOT)`：`WORKFLOW_DIR` 是 import 期从原始
    # ROOT 算出来的，而测试会 monkeypatch `gate.ROOT`；那时两者不再有父子关系，
    # `relative_to` 抛 ValueError，把一个只读的列举变成崩溃。
    if not WORKFLOW_DIR.is_dir():
        return []
    return sorted(
        f".github/workflows/{p.name}"
        for p in list(WORKFLOW_DIR.glob("*.yml")) + list(WORKFLOW_DIR.glob("*.yaml"))
    )


# ---- Which way did a change to the rules move? --------------------------------------
#
# A change to a self-governing file is the defendant editing the law, and the hazard is
# one-directional: the defendant rewriting it *in their own favour*. A change that can
# only make this gate say no more often is not in their favour, so it does not need the
# owner -- provided "more often" is computed from the two versions, never asserted in a
# pull request description.
#
# Only a closed set can carry that proof. `ci-gate-inventory.yaml` has a schema and feeds
# exactly one decision here (which checks must be green before a merge), so base-vs-head
# is a set comparison with a defined direction. Python and the rule prose have no such
# reading -- any line of either can loosen anything -- so they keep going to the owner.
# Unknown file, unreadable version, parse failure, empty base: all unproven. Loosening
# always goes to the owner. The default, in every branch below, is the owner.
#
# RULE_TEXT_FILES (AGENTS.md, ops.merge-gate.md) have a second, non-directional way
# out: a cited owner instruction in the PR body (`_owner_instruction_quoted`, applied
# in `evaluate`). That is not a direction proof and does not extend to this module's
# own code closure -- see the comment above `_OWNER_INSTRUCTION_HEADER_RE`.


def _blocking_coordinates(text: str) -> frozenset[tuple[str, str, str]] | None:
    """(gate id, workflow, job) for every gate with merge authority, or None.

    None, not an empty set, when the document cannot be read the way
    `_required_checks` reads it: "I could not tell" and "nothing blocks" must not
    collapse into the same value, because the second one makes every head a superset.
    """
    try:
        doc = yaml.safe_load(text)
        gates = (doc or {}).get("gates")
    except (yaml.YAMLError, AttributeError):
        return None
    if not isinstance(gates, list):
        return None
    out: set[tuple[str, str, str]] = set()
    for gate in gates:
        if not isinstance(gate, dict):
            return None
        if gate.get("blocks_merge"):
            out.add(
                (
                    str(gate.get("id", "")),
                    str(gate.get("workflow", "")),
                    str(gate.get("job", "")),
                )
            )
    return frozenset(out)


def _inventory_only_gained_authority(base_text: str, head_text: str) -> bool:
    """True when every check that blocked a merge before still blocks it, unchanged.

    Registering a gate is bookkeeping; `blocks_merge: true` is authority. A head whose
    blocking set is a superset of the base's cannot let anything through that the base
    would have stopped -- including when the two are equal, which is what a backfill of
    `blocks_merge: false` rows looks like. The coordinate carries the workflow and job,
    so repointing an existing blocking gate at a job that always passes is not a
    superset and is not proven.
    """
    base = _blocking_coordinates(base_text)
    head = _blocking_coordinates(head_text)
    if base is None or head is None:
        return False
    if not base:
        # An empty base makes every head a superset -- the same shape of hole as an
        # empty protected set, and it would prove exactly the change that empties it.
        return False
    return base <= head


# Self-governing paths a proof exists for. Everything else in the closure is unproven
# by construction, which is the point: this set only grows when someone can state what
# the file's decision input is and which way is stricter.
def _workflow_only_gained_authority(base_text: str, head_text: str) -> bool:
    """True 当这份 workflow 的改动只会让门禁更常说「不」。

    门禁从 workflow 里读两样东西，两样都得只增不减：

    * `on.push.paths` —— 哪些路径一合就部署。**多**一条 = 多一类改动要回 owner。
    * 每个 job 报告出来的检查名 —— `_required_checks()` 取 `name or job_id`，所以
      这里必须用同一个取法。只收有 `name:` 的 job 会让「给一个无名 job 加 name」
      看起来是超集，而它实际上把那条必需检查从 job id 改名了，旧名字从此不再报告。

    **`paths` 缺失不是空集**：GitHub Actions 把「没有 paths」当成「所有路径」。
    当成空集的话，「给一个本来无 paths 的 workflow 加上 paths 过滤」——一次收窄、
    一次放松——会被证明成收紧。这正是 `_inventory_only_gained_authority` 里防过的
    空集陷阱，在这里换了个形状（#809 review）。所以 paths 用 None 表示「全部」，
    并显式处理 base 全部 / head 收窄 这一组。

    其余随便改（`run:`、`env:`、新增 job……）都不影响这两个判定输入，不设限。
    """

    def read(text):
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError:
            return None
        if not isinstance(doc, dict):
            return None
        on = doc.get(True, doc.get("on"))
        push = on.get("push") if isinstance(on, dict) else None
        if isinstance(push, dict) and "paths" in push:
            raw = push["paths"]
            # 标量字符串会被 `frozenset(str(x) for x in raw)` 拆成字符集合。
            if not isinstance(raw, list):
                return None
            paths = frozenset(str(x) for x in raw)
        else:
            paths = None  # 没有 paths = 所有路径
        jobs = doc.get("jobs")
        if not isinstance(jobs, dict):
            return None
        names = frozenset(
            str((spec.get("name") if isinstance(spec, dict) else None) or job_id)
            for job_id, spec in jobs.items()
        )
        return paths, names

    base, head = read(base_text), read(head_text)
    if base is None or head is None:
        return False
    base_paths, head_paths = base[0], head[0]
    if base_paths is None:
        # base 触发于所有路径。只有 head 也触发于所有路径才不算放松。
        if head_paths is not None:
            return False
    elif head_paths is not None and not base_paths <= head_paths:
        return False
    return base[1] <= head[1]


DIRECTION_PROOFS = {
    "docs/ssot/ci-gate-inventory.yaml": _inventory_only_gained_authority,
}


# A push to main under these paths deploys (deploy.yml: the runner rebuild;
# deploy-cloudflare-watchdog.yml: `wrangler deploy` of the out-of-band worker, #718) —
# a merge must not be what triggers it under session authority.
# Paths whose merge sets something running that a revert does not undo. These
# are the ones no workflow declares for itself -- a runner rebuild, a bootstrap
# self-update -- so they stay written down.
DEPLOY_TRIGGERING_GLOBS = (
    "bootstrap/06.iac_runner/*",
    "bootstrap/06.iac_runner/**/*",
    "scripts/deploy_iac_runner_bootstrap.sh",
    ".github/workflows/deploy.yml",
    "cloudflare/infra-watchdog/*",
    "cloudflare/infra-watchdog/**/*",
)

# A workflow that pushes to main and does one of these deploys. Deliberately a
# short, explicit list: the judgement "this actually deploys" stays here, while
# the paths that reach it are derived, because it is the paths that drift.
# Workflows whose push-to-main deploy never reaches prod. Owner approval is
# scoped by environment (2026-09-21): staging, the reserved pr-0 canary slot and
# the report-branch-main preview are the agent's to merge; prod is not.
#
# This stays a written list because the question -- which environment does this
# deploy reach -- is not stated anywhere in the workflow file. It is the same
# shape as DEPLOY_MARKERS: the judgement is written down, the paths are derived.
NON_PROD_DEPLOY_WORKFLOWS = frozenset({"ops-checks.yml"})

DEPLOY_MARKERS = (
    "deploy_v2",
    "wrangler deploy",
    "iac_runner",
    "repository_dispatch",
    # apply-observability.yml pushes to main and runs
    # `invoke fr-observability.shared.apply-alerts` / `.apply-dashboard`
    # against the live SigNoz. AGENTS.md names "observability apply" as a
    # high-risk merge side effect by name, and the first version of this list
    # missed it -- the same omission it was written to stop.
    "invoke fr-observability",
    "terraform apply",
)
WORKFLOW_DIR = ROOT / ".github" / "workflows"


@functools.lru_cache(maxsize=1)
def _declared_deploy_globs() -> tuple[tuple[tuple[str, str], ...], bool]:
    """`on.push.paths` of every workflow that pushes to main and deploys.

    The hand-written list above missed `ops-checks.yml`, whose 21 push paths
    each start a live `deploy_v2` canary on merge -- its own comment says so:
    "Same-repo PRs, main pushes, schedules, and manual runs all mutate the same
    reserved ephemeral slot". A PR touching any of them therefore needs owner
    approval of that head under AGENTS.md's 高风险例外, and this gate would have
    said session authority sufficed.

    Deriving is the point. A second hand-written list would drift the same way
    the first one did; the workflows already declare which paths wake them.

    Reads only local files, so there is no network, no TTY and nothing to race.
    A workflow that cannot be parsed contributes nothing rather than raising --
    a malformed file must not take the gate offline -- and the written list
    above still stands on its own.
    """
    globs: list[tuple[str, str]] = []
    try:
        names = sorted(WORKFLOW_DIR.glob("*.y*ml"))  # Actions accepts .yaml too
    except OSError:
        return (), False
    if not names:
        # Path.glob swallows scandir errors, so a missing or unreadable
        # directory arrives here indistinguishable from an empty one -- which
        # is why the OSError branch above is effectively unreachable. An
        # existing-but-empty directory is a real repository state; one that is
        # not there at all is a broken read, and reporting it healthy silently
        # retired every derived deploy path.
        # is_dir() is True for a directory with no read permission, and
        # Path.glob swallows the PermissionError exactly as it swallows
        # FileNotFoundError -- so an unreadable directory reported healthy with
        # zero globs, silently retiring every derived deploy path. Probe the
        # read itself rather than the directory's existence.
        try:
            os.close(os.open(WORKFLOW_DIR, os.O_RDONLY))
            with os.scandir(WORKFLOW_DIR):
                pass
        except OSError:
            return (), False
        return (), True
    parsed = 0
    for path in names:
        try:
            text = path.read_text(encoding="utf-8")
            doc = yaml.safe_load(text)
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        parsed += 1
        if not isinstance(doc, dict):
            continue
        # PyYAML resolves a bare `on:` key to the boolean True.
        triggers = doc.get("on") if isinstance(doc.get("on"), dict) else doc.get(True)
        push = (triggers or {}).get("push") if isinstance(triggers, dict) else None
        if not isinstance(push, dict):
            continue
        # Actions accepts a bare string wherever it accepts a list, and an
        # omitted `branches` means every branch -- which includes main. Reading
        # only the list form silently skipped such a workflow, under-detecting
        # in the one direction that matters.
        # Actions: `tags` without `branches` means tag pushes only, so a merge
        # to main cannot start it. Reading an absent `branches` as "every
        # branch" is right in general and wrong here, and it was masked until
        # the `paths` case below stopped defaulting to nothing.
        if "tags" in push and "branches" not in push:
            continue
        if "branches" in push and "main" not in _as_list(push["branches"]):
            continue
        if not any(marker in text for marker in DEPLOY_MARKERS):
            continue
        paths = _as_list(push.get("paths"))
        if "paths" not in push:
            # Omitted `paths` means every path, the mirror of the `branches`
            # case above. Reading it as "no paths" made the broadest deploy
            # workflow contribute nothing -- coverage inverting with reach.
            paths = ["*", "**"]
        globs.extend((str(p), path.name) for p in paths)
    # Workflows present but none readable means the derivation is broken, not
    # that nothing deploys.
    # Every workflow must parse, not merely one of them. `parsed > 0` was the
    # first attempt and does not achieve the intent: with one file broken among
    # many, its paths vanish while the read still reports healthy, which is the
    # under-detection the derivation exists to prevent.
    return tuple(dict.fromkeys(globs)), parsed == len(names)


# gh's own classification of a check (`bucket`): pass / fail / pending / skipping /
# cancel. `state` (SUCCESS, SKIPPED, IN_PROGRESS, …) is kept as the fallback for a gh
# build without buckets.
# Merge states this gate accepts. Everything else is named by the
# mergeStateStatus reason itself -- including BLOCKED and BEHIND, which have no
# separate reason of their own. An earlier version of this comment claimed they
# did; adding a state here silently stops it blocking, so the list is the rule.
#
# HAS_HOOKS and UNSTABLE are accepted deliberately: the first is a repository
# configured with pre-receive hooks, the second means a non-required check is
# red. Required checks are already judged by name above, so treating UNSTABLE as
# a blocker here would duplicate that judgement and also block on checks the
# repository has decided do not block.
MERGE_STATES_OK = frozenset({"CLEAN", "HAS_HOOKS", "UNSTABLE"})

GREEN_BUCKETS = frozenset({"pass", "skipping"})


@functools.lru_cache(maxsize=1)
def _required_checks() -> tuple[frozenset[str], bool]:
    """Display names of the `blocks_merge: true` gates, and whether they read.

    This catches a required gate that never registered at all. It deliberately
    does NOT block on `skipping`: infra-ci.yml:69-82 states that skipping a
    required job via `if:` is a designed passing state -- a PR whose whole diff
    is Markdown needs none of those gates, and PRs #709 and #673 merged in
    exactly that shape. An earlier version of this function blocked on it, which
    made every docs-only PR unmergeable forever, including the one carrying this
    repository's own merge-authority rules.

    The case that version meant to catch -- `detect-changes` failing and taking
    its dependents down as `skipped` -- is already caught, because that job is
    itself red and `not_green` scans every check. The skip branch bought nothing
    and cost the designed path.

    The inventory stores a job key; `gh pr checks` reports the workflow's
    display name, so the name is read from the workflow rather than guessed.
    An unreadable inventory returns unhealthy, and the caller blocks on it:
    losing this list loses the only check that notices a gate never ran.
    """
    try:
        doc = yaml.safe_load(
            (ROOT / "docs/ssot/ci-gate-inventory.yaml").read_text(encoding="utf-8")
        )
        gates = [g for g in (doc.get("gates") or []) if g.get("blocks_merge")]
    except (OSError, yaml.YAMLError, AttributeError):
        return frozenset(), False
    if not gates:
        return frozenset(), False
    names: set[str] = set()
    for gate in gates:
        job, workflow = gate.get("job"), gate.get("workflow")
        display = job
        try:
            spec = yaml.safe_load((ROOT / str(workflow)).read_text(encoding="utf-8"))
            display = ((spec.get("jobs") or {}).get(job) or {}).get("name") or job
        except (OSError, yaml.YAMLError, AttributeError, TypeError):
            pass
        if display:
            names.add(str(display))
    return frozenset(names), True


GREEN_STATES = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})
MAX_REVIEW_THREADS = 100
# Enough to carry a thread's label; a thread that needs more replies than
# this to state its severity has a different problem.
MAX_THREAD_COMMENTS = 20
# gh's stderr when a pull request has no check registered yet (it exits 1, no JSON).
NO_CHECKS_REPORTED = "no checks reported"

Runner = Callable[[Sequence[str]], str]


@dataclass(frozen=True)
class HeadFacts:
    number: int
    state: str
    draft: bool
    base: str
    head_sha: str
    files: tuple[str, ...]
    last_push_at: float  # epoch seconds of the newest commit on the head
    checks: tuple[tuple[str, str], ...]  # (name, gh bucket or state)
    unresolved_threads: int
    # AGENTS.md's weighted total; see SEVERITY_WEIGHTS.
    unresolved_weight: float = 0.0
    review_threads_total: int = 0
    node_id: str = ""
    # GitHub's own merge computation. "UNKNOWN" means GitHub is still computing
    # a test merge and the caller should re-poll. ABSENT means `gh` was asked
    # for the field and did not return it -- a gh version change, a permission
    # downgrade, an API shape change -- which must block rather than pass: the
    # field's whole job is to block, so losing it silently loses the check.
    # "" is reserved for a HeadFacts built by hand, where the field was never
    # requested and there is nothing to have lost.
    mergeable: str = ""
    merge_state: str = ""
    # Files the base branch has changed since this head diverged from it. Their
    # intersection with `files` is what makes a green check stale.
    base_changed_files: tuple[str, ...] = ()
    # Self-governing files whose change was mechanically proven non-loosening
    # (`_proven_tighter`). Defaults to empty so a HeadFacts built by hand proves
    # nothing and every self-governing change in it goes to the owner.
    proven_tighter: tuple[str, ...] = ()
    # Rule files whose working-tree copy is not the base branch's
    # (`_working_tree_rule_drift`). Empty means "checked and identical"; a
    # hand-built HeadFacts therefore asserts no drift, which is what its other
    # fields do too.
    rule_drift: tuple[str, ...] = ()
    # What GitHub says the PR changes, against what `files` actually returned.
    # `gh pr view --json files` caps at 100; both the protected-file and the
    # deploy-triggering checks iterate `files`, so a larger PR silently drops
    # the owner gate. Measured on finance_report#2042: changedFiles=163,
    # files=100, and everything from docs/ libs/ scripts/ tools/ uv.lock
    # onward was past the cut.
    changed_files: int = 0
    # GitHub's own review verdict. The gate judged unresolved threads only, so a
    # reviewer clicking Request changes without leaving a resolvable thread was
    # invisible -- and with required_approving_review_count: 0 the ruleset does
    # not withhold the merge either, so mergeStateStatus stays CLEAN.
    review_decision: str = ""
    # Fields this call asked `gh` for and did not get back. Guarding one field
    # at a time left changedFiles with the old `or 0` treatment, and
    # `if facts.changed_files` then switched off the very guard that exists to
    # stop a truncated file list from dropping the owner gates -- a blind audit
    # showed the gate issuing `gh pr merge` on a 163-file PR because of it.
    absent_fields: tuple[str, ...] = ()
    # (reviewer login, commit reviewed, submitted epoch) — a review pins a head
    reviews: tuple[tuple[str, str, float], ...] = ()
    # The PR description, read for the owner-instruction citation that AGENTS.md
    # §6 lets RULE_TEXT_FILES substitute for a direction proof (see
    # `_owner_instruction_quoted`). Defaults to "" -- a hand-built HeadFacts
    # therefore cites nothing, which stays on the conservative side like every
    # other default here.
    body: str = ""

    def reviews_on_head(self) -> tuple[tuple[str, str, float], ...]:
        return tuple(r for r in self.reviews if r[1] == self.head_sha)


@dataclass
class Verdict:
    ready: bool
    owner_required: bool
    reasons: list[str] = field(default_factory=list)
    quiet_remaining_seconds: int = 0

    @property
    def exit_code(self) -> int:
        if self.ready:
            return 0
        return 2 if self.owner_required else 1


GH_TIMEOUT_S = 60


def _gh(argv: Sequence[str]) -> str:
    # This runs unattended, so stdin is closed rather than inherited: a gh that
    # decides to prompt (auth re-login, a confirmation) would otherwise block on
    # a terminal that is not there, and the gate would hang instead of failing.
    # The timeout is the same argument for the network.
    try:
        result = subprocess.run(
            ["gh", *argv],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=GH_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"gh {' '.join(argv)}: no response in {GH_TIMEOUT_S}s"
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(argv)}: {result.stderr.strip() or 'failed'}")
    return result.stdout


def _direction_proof_for(path: str):
    """这个文件的方向证明，没有则 None。

    workflow 走前缀匹配而不是逐个登记：闭包本身就是算出来的（`_all_workflow_files()`），
    再手写一份同样的清单，就是今天反复在拆的那种「两份会漂移的手写清单」。
    `DIRECTION_PROOFS` 留给逐个登记的个案。
    """
    if path in DIRECTION_PROOFS:
        return DIRECTION_PROOFS[path]
    if path.startswith(".github/workflows/"):
        return _workflow_only_gained_authority
    return None


def _file_at(repo: str, ref: str, path: str, *, gh: Runner = _gh) -> str | None:
    """A file's contents at a ref, or None if it cannot be read.

    Read from GitHub at both refs rather than from the working tree: the working tree
    is whatever happens to be checked out, and the question here is what *merging*
    would do to the rules.
    """
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


def _blob_sha(data: bytes) -> str:
    """A git blob id, computed locally so comparing a tree costs no extra API calls."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _working_tree_rule_drift(
    repo: str, base: str, *, gh: Runner = _gh
) -> tuple[str, ...]:
    """Self-governing files whose working-tree copy differs from the base branch's.

    This gate reads its own rules from the working tree. Nothing checked that those
    were the rules on the branch a merge would land on, so a checkout carrying an
    unmerged rule change issued verdicts under it -- on every pull request, including
    the ones that had nothing to do with the change.

    Measured, not hypothesised: on 2026-09-22 a background poller merged #791 while
    the working tree sat on the branch of #792, the still-unreviewed PR that relaxes
    exactly the rule #791 needed relaxed. #791's own content turned out to be
    non-loosening, so nothing was lost -- but the verdict was issued by a law that
    had not been enacted, which is the whole thing the self-adjudication rule exists
    to stop. Uncommitted edits to a rule file land in the same trap and are caught
    the same way.

    One API call: the base tree's blob ids, compared against locally computed ones.
    Anything unreadable counts as drift, because "I could not tell whether I am
    judging by the merged rules" has to stop a merge, not wave it through.
    """
    unknown = ("<the base branch's rules could not be read>",)
    if not (repo and base):
        return unknown
    try:
        payload = json.loads(
            gh(
                [
                    "api",
                    f"repos/{repo}/git/trees/{base}?recursive=1",
                    "--jq",
                    "{truncated:.truncated, tree:[.tree[]|{path,sha}]}",
                ]
            )
        )
        if payload.get("truncated"):
            # A truncated tree silently omits paths, and an omitted path compares
            # equal to nothing -- the same shape as the empty-set holes above.
            return unknown
        remote = {str(e["path"]): str(e["sha"]) for e in payload.get("tree") or []}
    except (RuntimeError, json.JSONDecodeError, KeyError, TypeError):
        return unknown
    if not remote:
        return unknown
    drift: list[str] = []
    for path in sorted(self_governing_files()):
        try:
            local = _blob_sha((ROOT / path).read_bytes())
        except OSError:
            drift.append(path)
            continue
        if remote.get(path) != local:
            drift.append(path)
    return tuple(drift)


def _proven_tighter(
    repo: str, base: str, head_sha: str, files: Sequence[str], *, gh: Runner = _gh
) -> tuple[str, ...]:
    """Self-governing files in `files` whose change is provably non-loosening.

    `base` is the base branch's tip, not the merge base: if main has since gained a
    blocking gate this head does not carry, merging would drop it, and the comparison
    against the tip is what notices.
    """
    proven: list[str] = []
    for path in sorted(set(files) & self_governing_files()):
        proof = _direction_proof_for(path)
        if proof is None:
            continue
        base_text = _file_at(repo, base, path, gh=gh)
        head_text = _file_at(repo, head_sha, path, gh=gh)
        if base_text is None or head_text is None:
            continue
        if proof(base_text, head_text):
            proven.append(path)
    return tuple(proven)


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _read_checks(number: int, *, repo: str, gh: Runner) -> str:
    """`gh pr checks --json`, with gh's "nothing registered yet" read as zero checks.

    Before the first check registers, gh exits 1 with "no checks reported on the
    '<branch>' branch" instead of printing `[]`. Raised as an error, that crashed the
    gate with a traceback (also exit 1), so the "no checks reported yet" reason below
    could never be reported. Any other failure still raises.
    """
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
    repo: str, head_sha: str, base: str, *, gh: Runner = _gh
) -> tuple[str, ...]:
    """Files the base branch has changed since this head diverged from it.

    `compare/<head>...<base>` is computed from the merge base, so it answers
    exactly "what has base gained that this head has not seen" -- not "what does
    the head change", which `files` already covers.

    This exists because a green check proves the tree it ran on, not the tree a
    merge would produce. A sibling PR that lands between the run and the merge
    can rewrite the very files under test, and every check stays green because
    none of them re-ran. finance_report's AGENTS.md states the same hazard for
    the adjacent field: `mergeStateStatus` "can flip from CLEAN to DIRTY/BEHIND
    the instant a sibling PR merges to main -- re-check it fresh before every
    'ready' report, never trust an earlier snapshot".

    Best-effort: a comparison that cannot be read yields no files, so this can
    only ever fail to raise a concern, never invent one. The hard blockers
    (`mergeable`, checks, threads) do not depend on it.
    """
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
    """A requested field's value, or ABSENT when it is missing OR empty.

    Keying on the key alone was one layer too shallow: `gh` returning
    `{"mergeable": null}` kept the key present, coalesced to "", and re-opened
    the fail-open. Both shapes mean the same thing -- the answer this call asked
    for did not arrive -- so both must block.
    """
    value = view.get(name)
    return str(value) if value else ABSENT


def collect(number: int, *, repo: str = DEFAULT_REPO, gh: Runner = _gh) -> HeadFacts:
    """Read the head's facts through `gh`; nothing here decides."""
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
                "{reviewThreads(first:%d){totalCount nodes{isResolved "
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
    review_threads = threads["data"]["repository"]["pullRequest"]["reviewThreads"]
    nodes = review_threads["nodes"]
    commits = view.get("commits") or []
    last_push = max((_epoch(c["committedDate"]) for c in commits), default=0.0)
    # The commits connection is capped at 100 and returned oldest-first, so on a
    # long branch the newest commit -- the one the quiet period is measured from
    # -- is exactly the one missing. And an empty list yields last_push = 0.0,
    # which reads as "pushed in 1970" and settles instantly. Both are caught by
    # asking whether the head itself came back.
    head_sha = str(view.get("headRefOid") or "")
    head_seen = any(str(c.get("oid") or "") == head_sha for c in commits)
    # Only an open pull request can be updated, so the comparison that feeds the
    # stale-green reason is worth a round trip only then. Auditing a run of
    # merged PRs would otherwise pay one extra API call each for an answer
    # `evaluate` discards.
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
    # Two API reads per provable file, so only for an open pull request that actually
    # touches one. A closed one is being audited, not merged.
    proven = (
        _proven_tighter(
            repo,
            str(view.get("baseRefName") or ""),
            str(view.get("headRefOid") or ""),
            changed,
            gh=gh,
        )
        if str(view.get("state") or "") == "OPEN"
        else ()
    )
    drift = (
        _working_tree_rule_drift(repo, str(view.get("baseRefName") or ""), gh=gh)
        if str(view.get("state") or "") == "OPEN"
        else ()
    )
    return HeadFacts(
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
        # `not view.get(name)`, not `name not in view`. _field() two lines up
        # was deliberately changed to treat a present-but-null value as ABSENT,
        # and the class guard introduced in the same commit did not carry that
        # over: {"files": null, "changedFiles": null} left the key present, gave
        # an empty file list, switched the truncation guard off, and every
        # owner gate then iterated nothing -- exit 0, `gh pr merge` issued.
        # None of these five is ever legitimately empty on a real pull request.
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
        # ABSENT, not "", when gh omits a field this call explicitly requested.
        mergeable=_field(view, "mergeable"),
        merge_state=_field(view, "mergeStateStatus"),
        base_changed_files=base_changed,
        body=str(view.get("body") or ""),
        reviews=tuple(
            (
                str((r.get("author") or {}).get("login") or ""),
                str((r.get("commit") or {}).get("oid") or ""),
                _epoch(r["submittedAt"]) if r.get("submittedAt") else 0.0,
            )
            for r in view.get("reviews") or []
        ),
    )


def request_copilot_review(facts: HeadFacts, *, gh: Runner = _gh) -> None:
    """Ask Copilot to review the current head (it re-reviews a fix-up push only on request)."""
    if not facts.node_id:
        raise RuntimeError("pull request node id unknown; cannot request a review")
    gh(
        [
            "api",
            "graphql",
            "-f",
            "query=mutation{requestReviews(input:{pullRequestId:%s,botIds:[%s],union:true})"
            "{pullRequest{number}}}"
            % (json.dumps(facts.node_id), json.dumps(COPILOT_BOT_ID)),
        ]
    )


def _deploy_triggering(path: str) -> str:
    """The workflow a merge of `path` would start, or "" for none.

    Returns the name rather than a bool so the verdict can say *what* fires. An
    owner asked to approve "tools/deploy_v2.py triggers a deploy" has to go and
    find out which one; "...starts ops-checks.yml" is a judgement they can make
    from the line itself.
    """
    # No translation is needed for `**`: unlike a shell glob, fnmatch's `*`
    # matches "/" as well, so "a/**" and "a/*" both already match "a/b/c.py".
    # An earlier version of this function called a _normalise() helper that
    # claimed to convert them and was in fact a no-op ("a/**"[:-1] + "*" is
    # "a/**"), which is worse than doing nothing: it read as though the case
    # were handled.
    for glob in DEPLOY_TRIGGERING_GLOBS:
        if fnmatch.fnmatch(path, glob):
            return "on merge"
    for glob, workflow in _declared_deploy_globs()[0]:
        if fnmatch.fnmatch(path, glob) and workflow not in NON_PROD_DEPLOY_WORKFLOWS:
            return workflow
    return ""


def _as_list(value: object) -> list[str]:
    """A YAML field that accepts a string or a list, read as a list either way."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def evaluate(
    facts: HeadFacts,
    *,
    now: float,
    quiet_minutes: int = QUIET_MINUTES,
    policy: str = "clock",
    settle_minutes: int = SETTLE_MINUTES,
) -> Verdict:
    """The rule, in one place. Every failed condition is a reason; owner-only
    conditions are named as such so a caller never waits on something time cannot fix."""
    reasons: list[str] = []
    owner = False
    if facts.state == "OPEN" and not facts.last_push_at:
        reasons.append(
            "no timestamp for the head commit, so the settling window cannot be "
            "measured: gh returned no commits, or the head was past the 100 it "
            "returns oldest-first"
        )
    if facts.state != "OPEN":
        reasons.append(f"pull request is {facts.state or 'unknown'}, not OPEN")
    if facts.draft:
        reasons.append("pull request is a draft")
    if facts.base != "main":
        reasons.append(f"base branch is {facts.base!r}, not main")
        owner = True
    # AGENTS.md's 合流真源唯一 names three things: the right base, `mergeable`,
    # and no conflict. Only the first was ever checked here, while the green
    # verdict printed the word "mergeable" without having asked GitHub. A
    # CONFLICTING pull request reached this function and was reported solely as
    # "N review thread(s) unresolved", which sends a caller to fix the wrong
    # thing.
    # Only meaningful while the pull request is open: GitHub stops computing a
    # test merge once it closes, so a merged head reports mergeable=UNKNOWN
    # forever. Asking anyway turned a one-line "not OPEN" verdict into four
    # lines of noise about a question that no longer has an answer.
    is_open = facts.state == "OPEN"
    absent = sorted(
        set(facts.absent_fields)
        | {
            name
            for name, value in (
                ("mergeable", facts.mergeable),
                ("mergeStateStatus", facts.merge_state),
            )
            if value == ABSENT
        }
    )
    if is_open and absent:
        reasons.append(
            f"gh did not return {', '.join(absent)} although asked for it: the "
            "conflict check is unavailable, so this cannot be judged ready"
        )
    # ABSENT is already named once above; letting it fall through would repeat
    # the same fact as three reasons and bury the one that explains it.
    if is_open and facts.mergeable == ABSENT:
        pass
    elif is_open and facts.mergeable == "CONFLICTING":
        reasons.append(
            "GitHub reports mergeable=CONFLICTING: rebase or merge main first"
        )
    elif is_open and facts.mergeable == "UNKNOWN":
        reasons.append(
            "GitHub is still computing mergeable (UNKNOWN): re-run in a moment "
            "rather than treating it as clean"
        )
    elif is_open and facts.mergeable and facts.mergeable != "MERGEABLE":
        reasons.append(f"GitHub reports mergeable={facts.mergeable}, not MERGEABLE")
    if (
        is_open
        and facts.merge_state
        and facts.merge_state != ABSENT
        and facts.merge_state not in MERGE_STATES_OK
    ):
        if facts.merge_state == "UNKNOWN":
            # Transient for the same reason mergeable=UNKNOWN is: GitHub is
            # still computing the test merge. Saying "not CLEAN" reads like a
            # settled conflict and sends the caller to rebase something that
            # may well be fine.
            reasons.append(
                "mergeStateStatus is still UNKNOWN: re-run in a moment rather "
                "than treating it as a conflict"
            )
        else:
            reasons.append(f"mergeStateStatus is {facts.merge_state}, not CLEAN")
    if facts.changed_files and len(facts.files) < facts.changed_files:
        reasons.append(
            f"gh returned {len(facts.files)} of {facts.changed_files} changed files "
            "(the API caps at 100): the protected-file and deploy checks read that "
            "list, so neither can be trusted here"
        )
        owner = True
    if facts.rule_drift:
        # Before asking what this pull request changes, ask whether the rules doing
        # the asking are the merged ones. They are read from the working tree, so a
        # checkout on another branch -- or with an uncommitted edit -- judges every
        # pull request by a law nobody has enacted.
        reasons.append(
            f"the working tree's copy of the merge rules is not "
            f"{facts.base}'s ({', '.join(facts.rule_drift)}): every verdict here "
            f"would be issued under rules that are not merged — check out {facts.base} "
            f"cleanly and re-run, or land those changes first"
        )
        owner = True

    quoted_instruction = _owner_instruction_quoted(facts.body)
    governing = sorted(f for f in facts.files if f in self_governing_files())
    unproven = [
        f
        for f in governing
        if f not in facts.proven_tighter
        # RULE_TEXT_FILES get a second way to clear this check: a cited owner
        # instruction in the PR body, verifiable by `_owner_instruction_quoted`
        # rather than merely claimed. Everything else in the closure has no such
        # carve-out -- see the comment above `_OWNER_INSTRUCTION_HEADER_RE`.
        and not (f in RULE_TEXT_FILES and quoted_instruction)
    ]
    if unproven:
        # Proven-tighter files are excluded, not the whole check: a PR that tightens the
        # inventory and edits the gate's Python still needs the owner for the Python.
        reasons.append(
            f"changes what decides merges ({', '.join(unproven)}) without a mechanical "
            f"proof that the change can only make this gate say no more often: the "
            f"working-tree copy is what judged this PR, so owner approval of head "
            f"{facts.head_sha[:7]} is required"
        )
        if any(f in RULE_TEXT_FILES for f in unproven):
            reasons.append(
                "rule-text files (AGENTS.md / docs/ssot/ops.merge-gate.md) can clear "
                "this instead by citing the owner instruction that authorised the "
                "edit in the PR body, under a heading matching 'owner instruction' / "
                "'owner 指示' followed by a quoted line (`> ...` or 「...」)"
            )
        owner = True
    if not _declared_deploy_globs()[1]:
        reasons.append(
            "cannot read .github/workflows to determine which paths deploy on "
            "merge: fix the read rather than merging on an unknown"
        )
        # exit 1 means "not yet"; a poller retries it forever. Not knowing
        # whether a merge deploys is precisely the case that must escalate.
        owner = True
    fired = {f: _deploy_triggering(f) for f in facts.files}
    deploying = sorted(f for f, w in fired.items() if w)
    if deploying:
        workflows = sorted({fired[f] for f in deploying if fired[f] != "on merge"})
        via = f" via {', '.join(workflows)}" if workflows else ""
        reasons.append(
            f"merging would trigger a deploy{via} ({', '.join(deploying)}): owner "
            f"approval of head {facts.head_sha[:7]} required"
        )
        owner = True
    not_green = sorted(
        name
        for name, verdict in facts.checks
        if verdict not in GREEN_BUCKETS and verdict not in GREEN_STATES
    )
    required, inventory_read = _required_checks()
    if not inventory_read:
        reasons.append(
            "cannot read docs/ssot/ci-gate-inventory.yaml: the required-check "
            "list is unavailable, so a gate that never ran cannot be noticed"
        )
    reported = {name for name, _ in facts.checks}
    missing = sorted(required - reported)
    if missing:
        reasons.append(f"required check(s) never reported: {', '.join(missing)}")
    # `skipping` is the designed state for a docs-only PR, and blocking on it
    # outright made those permanently unmergeable. But the gate still has to
    # tell 适用 from 意外 skipped, and the workflow's own answer cannot be
    # trusted for it: infra-ci computes has_non_doc from `git diff` against the
    # base, so a rewritten base yields a wrong file list, emits
    # has_non_doc=false, and reports Detect Non-Doc Changes GREEN while every
    # required gate skips. GitHub's own file list for the PR is independent of
    # that computation, so it is what decides here.
    if any(not f.endswith(".md") for f in facts.files):
        skipped = sorted(
            name
            for name, verdict in facts.checks
            if name in required and verdict in ("skipping", "SKIPPED")
        )
        if skipped:
            reasons.append(
                f"required check(s) skipped although this PR changes non-Markdown "
                f"files: {', '.join(skipped)}"
            )
    if not facts.checks:
        reasons.append("no checks reported yet")
    if not_green:
        reasons.append(f"check(s) not green: {', '.join(not_green)}")
    # Green proves the tree the checks ran on. When main has since rewritten
    # files this pull request also touches, that tree is not the one a merge
    # would produce, and nothing re-ran to notice.
    stale = sorted(set(facts.files) & set(facts.base_changed_files))
    if stale and not not_green and is_open:
        shown = ", ".join(stale[:4]) + (
            f" (+{len(stale) - 4} more)" if len(stale) > 4 else ""
        )
        reasons.append(
            f"checks are green against a base that has since changed {len(stale)} of "
            f"this PR's files ({shown}): update the branch so they re-run"
        )
    if facts.review_decision == "CHANGES_REQUESTED":
        reasons.append(
            "a reviewer has requested changes: resolve it with them rather than "
            "merging over it"
        )
    if facts.unresolved_weight >= BLOCKING_SEVERITY_TOTAL:
        reasons.append(
            f"{facts.unresolved_threads} unresolved review thread(s) weigh "
            f"{facts.unresolved_weight:g} (AGENTS.md blocks at "
            f"{BLOCKING_SEVERITY_TOTAL:g}; unlabelled counts as middle)"
        )
    if facts.review_threads_total > MAX_REVIEW_THREADS:
        reasons.append(
            f"{facts.review_threads_total} review threads, only the first "
            f"{MAX_REVIEW_THREADS} were read: resolve or evaluate by hand"
        )
    if policy not in ("clock", "event", "either"):
        raise ValueError(f"unknown policy {policy!r}")
    # Round up: a fractional second still inside the window is inside the window.
    clock_remaining = math.ceil(facts.last_push_at + quiet_minutes * 60 - now)
    clock_reason = (
        f"head pushed {int(now - facts.last_push_at)}s ago; quiet period has "
        f"{clock_remaining}s to run"
    )
    # A review without a submission time is not a submitted review (r[2] == 0.0 would
    # read as "settled since the epoch"): only timestamped automated reviews count.
    automated = [
        r for r in facts.reviews_on_head() if r[0] in AUTOMATED_REVIEWERS and r[2] > 0
    ]
    if automated:
        reviewed_at = max(r[2] for r in automated)
        event_remaining = math.ceil(reviewed_at + settle_minutes * 60 - now)
        event_reason = (
            f"head reviewed {int(now - reviewed_at)}s ago; settling for "
            f"{event_remaining}s more"
        )
    else:
        event_remaining = None  # no review: the event never happened
        event_reason = (
            f"no automated review on head {facts.head_sha[:7]} yet "
            "(--request-review asks Copilot for one)"
        )
    remaining = 0
    if policy == "clock" and clock_remaining > 0:
        remaining = clock_remaining
        reasons.append(clock_reason)
    elif policy == "event" and (event_remaining is None or event_remaining > 0):
        remaining = event_remaining or 0
        reasons.append(event_reason)
    elif policy == "either":
        event_ok = event_remaining is not None and event_remaining <= 0
        if clock_remaining > 0 and not event_ok:
            remaining = (
                clock_remaining
                if event_remaining is None
                else min(clock_remaining, event_remaining)
            )
            reasons.append(f"{event_reason}; {clock_reason}")
    return Verdict(
        ready=not reasons,
        owner_required=owner,
        reasons=reasons,
        quiet_remaining_seconds=max(0, remaining),
    )


def render(facts: HeadFacts, verdict: Verdict) -> str:
    head = f"#{facts.number} @ {facts.head_sha[:7]}"
    if verdict.ready:
        return (
            f"{head}: mergeable under session authority — checks green "
            f"({len(facts.checks)}), threads resolved, settled, "
            "no protected or deploy-triggering paths"
        )
    kind = "needs the owner" if verdict.owner_required else "not yet"
    return f"{head}: {kind}\n" + "\n".join(f"  - {r}" for r in verdict.reasons)


def main(argv: list[str] | None = None, *, gh: Runner = _gh, now=time.time) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("number", type=int)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--quiet-minutes", type=int, default=QUIET_MINUTES)
    parser.add_argument(
        "--merge", action="store_true", help="squash-merge when the verdict is ready"
    )
    parser.add_argument(
        "--policy",
        choices=("clock", "event", "either"),
        default="clock",
        help="settling rule: 12 min after the push (clock), 3 min after an automated "
        "review of the head (event), or whichever comes first (either)",
    )
    parser.add_argument(
        "--request-review",
        action="store_true",
        help="ask Copilot to review the head when no automated review of it exists",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="run OMCA fast audit and block merge on critical architectural or blindfold findings",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable verdict")
    args = parser.parse_args(argv)

    from libs.console import error, success, warning

    facts = collect(args.number, repo=args.repo, gh=gh)
    if args.request_review and not any(
        r[0] in AUTOMATED_REVIEWERS and r[2] > 0 for r in facts.reviews_on_head()
    ):
        request_copilot_review(facts, gh=gh)
        warning(f"#{facts.number}: Copilot review requested on {facts.head_sha[:7]}")
    verdict = evaluate(
        facts, now=now(), quiet_minutes=args.quiet_minutes, policy=args.policy
    )
    if args.audit:
        try:
            from tools.omca_gate_policy import evaluate_audit_report

            audit_proc = subprocess.run(
                ["omca", "audit", "--json", "--mode", "fast"],
                capture_output=True,
                text=True,
                check=False,
            )
            if audit_proc.returncode != 0:
                verdict.reasons.append(
                    f"omca audit failed to run (rc={audit_proc.returncode}): {audit_proc.stderr.strip()[:100]}"
                )
                verdict.ready = False
            else:
                report = json.loads(audit_proc.stdout)
                passed, blocking, _ = evaluate_audit_report(
                    report, expect_sha=facts.head_sha
                )
                if not passed:
                    for b in blocking:
                        verdict.reasons.append(f"omca audit blocked: {b}")
                    verdict.ready = False
        except Exception as exc:
            verdict.reasons.append(f"omca audit execution error: {exc}")
            verdict.ready = False
    if args.json:
        print(
            json.dumps(
                {
                    "number": facts.number,
                    "head": facts.head_sha,
                    "ready": verdict.ready,
                    "owner_required": verdict.owner_required,
                    "reasons": verdict.reasons,
                    "quiet_remaining_seconds": verdict.quiet_remaining_seconds,
                    "policy": args.policy,
                }
            )
        )
    else:
        text = render(facts, verdict)
        if verdict.ready:
            success(text)
        elif verdict.owner_required:
            error(text)
        else:
            warning(text)
    if verdict.ready and args.merge:
        gh(
            [
                "pr",
                "merge",
                str(facts.number),
                "--repo",
                args.repo,
                "--squash",
                "--delete-branch",
                "--match-head-commit",
                facts.head_sha,
            ]
        )
        success(f"merged #{facts.number} at {facts.head_sha[:7]}")
    return verdict.exit_code


if __name__ == "__main__":
    sys.exit(main())
