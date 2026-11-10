"""Read-only view of the production promotion queue (#1528).

Production promotions share the concurrency group `iac-input-reconcile-prod`.
A run that waits for approval holds the group. A new dispatch replaces the
pending run, and GitHub cancels the replaced run. This module prints the queue.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

Runner = Callable[..., subprocess.CompletedProcess]

WORKFLOW_FILE = "reconcile-iac-inputs.yml"
ACTIVE_STATUSES = ("requested", "queued", "pending", "waiting", "in_progress")
# The `run-name` of the workflow is `Reconcile IaC Inputs (PROD <tag>)` for a promotion.
PROD_TITLE = re.compile(r"\(PROD\s+(?P<tag>[^\s)]+)\)")

EXIT_EMPTY = 0
EXIT_OCCUPIED = 1
EXIT_UNREADABLE = 4


@dataclass(frozen=True)
class ProductionRun:
    run_id: int
    tag: str
    status: str
    created_at: str
    age_minutes: int
    url: str


@dataclass(frozen=True)
class ProductionQueue:
    runs: tuple[ProductionRun, ...] = ()
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"runs": [asdict(run) for run in self.runs], "error": self.error}


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _read_status(
    root: Path, status: str, runner: Runner, now: datetime
) -> list[ProductionRun]:
    endpoint = (
        f"repos/{{owner}}/{{repo}}/actions/workflows/{WORKFLOW_FILE}/runs"
        f"?status={status}&per_page=100"
    )
    completed = runner(
        ["gh", "api", endpoint], cwd=root, capture_output=True, text=True
    )
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip() or f"exit {completed.returncode}"
        raise ValueError(f"gh api status={status} failed: {detail}")
    payload = json.loads(completed.stdout)
    runs: list[ProductionRun] = []
    for raw in payload["workflow_runs"]:
        match = PROD_TITLE.search(raw["display_title"])
        if match is None:
            continue
        created = _parse_time(raw["created_at"])
        runs.append(
            ProductionRun(
                run_id=int(raw["id"]),
                tag=match.group("tag"),
                status=str(raw["status"]),
                created_at=str(raw["created_at"]),
                age_minutes=max(0, int((now - created).total_seconds() // 60)),
                url=str(raw["html_url"]),
            )
        )
    return runs


def read_production_queue(
    root: Path,
    *,
    runner: Runner = subprocess.run,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> ProductionQueue:
    """Active production promotion runs. An unreadable queue is never empty."""
    observed = now()
    found: dict[int, ProductionRun] = {}
    try:
        for status in ACTIVE_STATUSES:  # one call per status: no old run is paged out
            for run in _read_status(root, status, runner, observed):
                found.setdefault(run.run_id, run)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return ProductionQueue(error=f"{type(exc).__name__}: {exc}")
    ordered = sorted(found.values(), key=lambda run: (run.created_at, run.run_id))
    return ProductionQueue(runs=tuple(ordered))


def render_queue_lines(queue: ProductionQueue) -> list[str]:
    if queue.error:
        return [f"prod_queue: UNREADABLE ({queue.error})"]
    if not queue.runs:
        return ["prod_queue: empty"]
    return [
        f"prod_queue: run {run.run_id} PROD {run.tag} status={run.status} "
        f"age={run.age_minutes}m {run.url}"
        for run in queue.runs
    ]


def preflight(queue: ProductionQueue) -> tuple[int, list[str]]:
    """Exit 0 when no production run is active, 1 when one is, 4 when unreadable."""
    if queue.error:
        return EXIT_UNREADABLE, [
            f"prod-queue preflight: UNREADABLE ({queue.error}). Do not dispatch."
        ]
    if not queue.runs:
        return EXIT_EMPTY, ["prod-queue preflight: empty. A dispatch is allowed."]
    lines = [
        "prod-queue preflight: BLOCKED. A production run is active. "
        "Do not dispatch: a new dispatch cancels a pending run."
    ]
    for run in queue.runs:
        lines.append(
            f"  run {run.run_id} PROD {run.tag} status={run.status} "
            f"age={run.age_minutes}m {run.url}"
        )
    lines.append("Remedy: wait until each run completes.")
    lines.append(
        "A run that waits for approval and that no live session started may be "
        "cancelled only with an owner reply: gh run cancel <id> --repo wangzitian0/infra2"
    )
    return EXIT_OCCUPIED, lines


__all__ = [
    "ACTIVE_STATUSES",
    "PROD_TITLE",
    "ProductionQueue",
    "ProductionRun",
    "preflight",
    "read_production_queue",
    "render_queue_lines",
]
