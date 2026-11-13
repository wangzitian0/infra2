"""Replay merged PRs through the head's gate and the gate of origin/main (#1653).

Both gates judge the same normalized facts, so a changed verdict comes only from the
gate code. Each gate runs in its own process in its own worktree: `evaluate()` reads
the working tree (workflows, inventory, closure)."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import fields, replace
from pathlib import Path

from common.core.ref_worktree import run_at_ref
from common.gate.client import collect
from common.gate.types import ROOT, HeadFacts, Runner, _epoch

REPLAY_TIMEOUT_S = 120
# The settling policy of the merge command (`--policy either`): replay what a merge sees.
MERGE_POLICY = "either"
# One process of one gate: read `[{"facts", "now"}]` on stdin, print one verdict each.
# It uses only names that origin/main has, so a main without this module can run it.
_EVALUATE_SCRIPT = """
import json, sys
from common.gate.evaluator import evaluate
from common.gate.types import HeadFacts

def tuples(value):
    return tuple(tuples(v) for v in value) if isinstance(value, list) else value

out = []
payload = json.load(sys.stdin)
for item in payload["items"]:
    facts = HeadFacts(**{k: tuples(v) for k, v in item["facts"].items()})
    v = evaluate(facts, now=item["now"], policy=payload["policy"])
    out.append({"ready": v.ready, "exit_code": v.exit_code, "reasons": list(v.reasons)})
print("REPLAY_VERDICTS " + json.dumps(out))
"""

# (facts, now) in; one verdict dict each out.
Evaluator = Callable[[Sequence[tuple[HeadFacts, float]]], list[dict]]


def normalize(facts: HeadFacts, merged_at: float) -> tuple[HeadFacts, float]:
    """The facts as the gate saw an open, clean PR at the merge time, and that time."""
    return (
        replace(
            facts,
            state="OPEN",
            mergeable="MERGEABLE",
            merge_state="CLEAN",
            lock_failures=(),
            comments=tuple(c for c in facts.comments if c[1] <= merged_at),
            reviews=tuple(r for r in facts.reviews if r[2] <= merged_at),
        ),
        merged_at,
    )


def fetch(count: int, *, repo: str, gh: Runner) -> list[tuple[HeadFacts, float]]:
    """Normalized facts of the last ``count`` merged PRs into main."""
    listing = json.loads(
        gh(
            [
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "merged",
                "--base",
                "main",
                "--limit",
                str(count),
                "--json",
                "number,mergedAt",
            ]
        )
    )
    return [
        normalize(collect(int(pr["number"]), repo=repo, gh=gh), _epoch(pr["mergedAt"]))
        for pr in listing
    ]


def evaluate_in_process_of(ref: str | None, *, root: Path = ROOT) -> Evaluator:
    """An evaluator that runs the gate of ``ref`` (None: the checkout at ``root``)."""

    def run(items: Sequence[tuple[HeadFacts, float]]) -> list[dict]:
        payload = json.dumps(
            {
                "policy": MERGE_POLICY,
                "items": [
                    {
                        "facts": {
                            f.name: getattr(facts, f.name) for f in fields(facts)
                        },
                        "now": now,
                    }
                    for facts, now in items
                ],
            }
        )
        args = [sys.executable, "-c", _EVALUATE_SCRIPT]
        if ref is None:
            proc = subprocess.run(
                args,
                cwd=root,
                input=payload,
                capture_output=True,
                text=True,
                timeout=REPLAY_TIMEOUT_S,
            )
        else:
            proc = run_at_ref(
                ref, args, root=root, timeout=REPLAY_TIMEOUT_S, stdin=payload
            )
        line = next(
            (x for x in proc.stdout.splitlines() if x.startswith("REPLAY_VERDICTS ")),
            None,
        )
        if proc.returncode != 0 or line is None:
            raise RuntimeError(
                f"the gate of {ref or 'the head'} did not run: {proc.stderr.strip()[-300:]}"
            )
        return json.loads(line.removeprefix("REPLAY_VERDICTS "))

    return run


def _direction(main: dict, head: dict) -> str:
    if main["ready"] != head["ready"]:
        return "looser" if head["ready"] else "stricter"
    added = set(head["reasons"]) - set(main["reasons"])
    removed = set(main["reasons"]) - set(head["reasons"])
    if added and removed:
        return "mixed"
    return "stricter" if added else "looser" if removed else "equal"


def compare(
    items: Sequence[tuple[HeadFacts, float]], *, main: Evaluator, head: Evaluator
) -> dict:
    """``{"direction", "rows"}``: one row for each PR whose verdict changed."""
    if not items:
        raise ValueError("the replay set is empty: no verdict to compare")
    main_verdicts, head_verdicts = main(items), head(items)
    rows = []
    # strict: a short verdict list raises, so a replay never passes while empty.
    for (facts, _), m, h in zip(items, main_verdicts, head_verdicts, strict=True):
        direction = _direction(m, h)
        if direction != "equal":
            rows.append(
                {
                    "number": facts.number,
                    "main": m["exit_code"],
                    "head": h["exit_code"],
                    "reasons_added": sorted(set(h["reasons"]) - set(m["reasons"])),
                    "reasons_removed": sorted(set(m["reasons"]) - set(h["reasons"])),
                    "direction": direction,
                }
            )
    directions = {r["direction"] for r in rows}
    return {
        "replayed": len(items),
        "direction": directions.pop()
        if len(directions) == 1
        else "mixed"
        if directions
        else "equal",
        "rows": rows,
    }


def run_replay(count: int, *, repo: str, gh: Runner, root: Path = ROOT) -> dict:
    """The replay of the last ``count`` merged PRs: head checkout against origin/main."""
    if count < 1:
        raise ValueError(f"the replay count must be at least 1, not {count}")
    # A stale or shallow ref would compare against an old main.
    subprocess.run(
        ["git", "fetch", "--quiet", "origin", "main"],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=REPLAY_TIMEOUT_S,
    )
    items = fetch(count, repo=repo, gh=gh)
    return compare(
        items,
        main=evaluate_in_process_of("origin/main", root=root),
        head=evaluate_in_process_of(None, root=root),
    )
