"""Data types, constants, and environment accessors for the merge gate."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
ABSENT = "ABSENT"
DEFAULT_REPO = "wangzitian0/infra2"

QUIET_MINUTES = 12
SETTLE_MINUTES = 3  # event policy: after the review of the head, not after the push
COPILOT_BOT_ID = "BOT_kgDOCnlnWA"
AUTOMATED_REVIEWERS = frozenset({"copilot-pull-request-reviewer"})

RULE_TEXT_FILES = ("AGENTS.md", "docs/ssot/ops.merge-gate.md")

SEVERITY_WEIGHTS = {"high": 1.0, "middle": 0.5, "medium": 0.5, "low": 0.25}
UNLABELLED_SEVERITY_WEIGHT = SEVERITY_WEIGHTS["middle"]
BLOCKING_SEVERITY_TOTAL = 1.0

WORKFLOW_PREFIX = ".github/workflows/"
WORKFLOW_DIR = ROOT / ".github" / "workflows"

DEPLOY_TRIGGERING_GLOBS = (
    "bootstrap/06.iac_runner/*",
    "bootstrap/06.iac_runner/**/*",
    "scripts/deploy_iac_runner_bootstrap.sh",
    ".github/workflows/deploy.yml",
    "cloudflare/infra-watchdog/*",
    "cloudflare/infra-watchdog/**/*",
)

NON_PROD_DEPLOY_WORKFLOWS = frozenset({"ops-checks.yml"})

DEPLOY_MARKERS = (
    "deploy_v2",
    "wrangler deploy",
    "iac_runner",
    "repository_dispatch",
    "invoke fr-observability",
    "terraform apply",
)

MERGE_STATES_OK = frozenset({"CLEAN", "HAS_HOOKS", "UNSTABLE"})
GREEN_BUCKETS = frozenset({"pass", "skipping"})
GREEN_STATES = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})
MAX_REVIEW_THREADS = 100
MAX_THREAD_COMMENTS = 20
NO_CHECKS_REPORTED = "no checks reported"
GH_TIMEOUT_S = 200

Runner = Callable[[Sequence[str]], str]


def _get_root() -> Path:
    """Dynamic resolver for repository root, honoring test monkeypatches on tools.pr_merge_gate."""
    gate_mod = sys.modules.get("tools.pr_merge_gate")
    if gate_mod and hasattr(gate_mod, "ROOT"):
        return Path(gate_mod.ROOT)
    return ROOT


def _get_workflow_dir() -> Path:
    """Dynamic resolver for workflow directory, honoring test monkeypatches."""
    gate_mod = sys.modules.get("tools.pr_merge_gate")
    if gate_mod and hasattr(gate_mod, "WORKFLOW_DIR"):
        return Path(gate_mod.WORKFLOW_DIR)
    return WORKFLOW_DIR


def _get_yaml():
    """Dynamic resolver for yaml parser, honoring test monkeypatches."""
    gate_mod = sys.modules.get("tools.pr_merge_gate")
    if gate_mod and hasattr(gate_mod, "yaml"):
        return gate_mod.yaml
    import yaml

    return yaml


def _get_subprocess():
    """Dynamic resolver for subprocess module, honoring test monkeypatches."""
    gate_mod = sys.modules.get("tools.pr_merge_gate")
    if gate_mod and hasattr(gate_mod, "subprocess"):
        return gate_mod.subprocess
    return subprocess


def _repo_slug(repo: str) -> str:
    """Normalize 'owner/name' or git remote URL into 'owner/name'."""
    repo = repo.removesuffix(".git").strip()
    if "/" in repo:
        parts = repo.split("/")
        return f"{parts[-2]}/{parts[-1]}"
    return repo


def _is_local_root_repo(repo: str) -> bool:
    """True when `repo` is the repository rooted at `ROOT` (infra2)."""
    return _repo_slug(repo).lower() == _repo_slug(DEFAULT_REPO).lower()


def _as_list(value: object) -> list[str]:
    """A YAML field that accepts a string or a list, read as a list either way."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


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
    unresolved_weight: float = 0.0
    review_threads_total: int = 0
    repo: str = DEFAULT_REPO
    node_id: str = ""
    mergeable: str = ""
    merge_state: str = ""
    base_changed_files: tuple[str, ...] = ()
    proven_tighter: tuple[str, ...] = ()
    rule_drift: tuple[str, ...] = ()
    changed_files: int = 0
    review_decision: str = ""
    absent_fields: tuple[str, ...] = ()
    reviews: tuple[tuple[str, str, float], ...] = ()
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
