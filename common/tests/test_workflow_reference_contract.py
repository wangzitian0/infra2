from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath

import yaml

ROOT = Path(__file__).resolve().parents[2]

WORKFLOW_REFERENCE_RE = re.compile(
    r"(?<![\w-])(?:\.\./)*(?:\./)?\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml"
)
OFFICIAL_ACTION_USE_RE = re.compile(
    r"^[ \t]*uses:[ \t]*(actions/[A-Za-z0-9_.-]+)@([^\s#]+)"
    r"(?:[ \t]*#.*)?$",
    re.MULTILINE,
)
ACTION_VERSION_RE = re.compile(r"v(\d+)(?:\.\d+\.\d+)?")
# These majors use the supported Node.js 24 action runtime. Raising a baseline is
# deliberate; every workflow is scanned so newly added files cannot bypass it.
MINIMUM_OFFICIAL_ACTION_MAJORS = {
    "actions/checkout": 7,
    "actions/deploy-pages": 5,
    "actions/setup-go": 6,
    "actions/setup-python": 6,
    "actions/upload-artifact": 7,
    "actions/upload-pages-artifact": 5,
}
SCANNED_SUFFIXES = {
    ".md",
    ".py",
    ".sh",
    ".txt",
    ".yaml",
    ".yml",
}
IGNORED_TOP_LEVEL_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "e2e_regressions",
    "playground",
    "repos",
}
# Workflow files that legitimately live in an APP repo, not infra2 — referenced
# here because the decentralized Production evidence contracts (#576) declare
# per-app workflow paths that infra2's receiver verifies against the app's own
# repo. The app repos' own contract tests (truealpha PR #465, finance_report
# PR #1978) assert these files actually exist there; this gate only guards
# references to INFRA2's workflows going stale.
FOREIGN_APP_WORKFLOWS = {
    ".github/workflows/ci-required.yml",  # truealpha (source build)
    ".github/workflows/deploy-release.yml",  # truealpha (staging deploy)
    # truealpha (watchdog of watchdogs); ops-checks' peer check reads it live and
    # goes red if it is gone (common/observability/scheduler_peer_liveness.py, truealpha#876).
    ".github/workflows/scheduler-liveness.yml",
}


def _is_scanned(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    if relative.parts[0] in IGNORED_TOP_LEVEL_DIRS:
        return False
    return path.is_file() and path.suffix in SCANNED_SUFFIXES


def _normalize_workflow_reference(reference: str) -> str:
    return reference[reference.index(".github/workflows/") :]


def _official_action_violations(path: Path, content: str) -> list[str]:
    violations: list[str] = []
    for match in OFFICIAL_ACTION_USE_RE.finditer(content):
        action, ref = match.groups()
        line_no = content.count("\n", 0, match.start()) + 1
        minimum = MINIMUM_OFFICIAL_ACTION_MAJORS.get(action)
        if minimum is None:
            violations.append(
                f"{path}:{line_no}: {action}@{ref} has no governed minimum"
            )
            continue

        version = ACTION_VERSION_RE.fullmatch(ref)
        if version is None:
            violations.append(
                f"{path}:{line_no}: {action}@{ref} must use a governed version"
            )
            continue
        if int(version.group(1)) < minimum:
            violations.append(f"{path}:{line_no}: {action}@{ref} requires v{minimum}+")
    return violations


def test_workflow_reference_matcher_accepts_relative_markdown_targets() -> None:
    content = (
        "[deploy](../../.github/workflows/deploy.yml)\n"
        "[docs](./.github/workflows/docs.yml)\n"
        "[infra-ci](/.github/workflows/infra-ci.yml)\n"
    )
    references = [
        _normalize_workflow_reference(match.group(0))
        for match in WORKFLOW_REFERENCE_RE.finditer(content)
    ]
    assert references == [
        ".github/workflows/deploy.yml",
        ".github/workflows/docs.yml",
        ".github/workflows/infra-ci.yml",
    ]


def test_workspace_submodules_are_outside_infra_workflow_discovery() -> None:
    workspace_readme = ROOT / "repos" / "README.md"
    assert workspace_readme.is_file()
    assert not _is_scanned(workspace_readme)


def test_official_action_guard_rejects_stale_unversioned_and_unknown_actions() -> None:
    content = "\n".join(
        [
            "    uses: actions/checkout@v4",
            "    uses: actions/setup-python@main",
            "    uses: actions/unknown@v1",
        ]
    )

    assert _official_action_violations(Path("fixture.yml"), content) == [
        "fixture.yml:1: actions/checkout@v4 requires v7+",
        "fixture.yml:2: actions/setup-python@main must use a governed version",
        "fixture.yml:3: actions/unknown@v1 has no governed minimum",
    ]


def test_workflows_use_governed_supported_official_action_majors() -> None:
    violations: list[str] = []
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))

    for workflow in workflows:
        content = workflow.read_text(encoding="utf-8")
        violations.extend(
            _official_action_violations(workflow.relative_to(ROOT), content)
        )

    assert violations == [], "Ungoverned/unsupported GitHub Actions:\n" + "\n".join(
        violations
    )


def test_workflow_references_point_to_live_workflow_files() -> None:
    """Workflow docs/tests must not point at retired workflow files."""
    live_workflows = {
        workflow.relative_to(ROOT).as_posix()
        for workflow in (ROOT / ".github" / "workflows").glob("*.yml")
    } | {
        workflow.relative_to(ROOT).as_posix()
        for workflow in (ROOT / ".github" / "workflows").glob("*.yaml")
    }

    missing: list[str] = []
    for path in sorted(ROOT.rglob("*")):
        if any(
            part.startswith(".") and part != ".github"
            for part in path.relative_to(ROOT).parts
        ):
            continue  # hidden dirs (.claude worktrees are full repo copies) — but keep .github
        if not _is_scanned(path):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for match in WORKFLOW_REFERENCE_RE.finditer(content):
            reference = _normalize_workflow_reference(match.group(0))
            if reference in FOREIGN_APP_WORKFLOWS:
                continue
            if reference not in live_workflows:
                line_no = content.count("\n", 0, match.start()) + 1
                missing.append(f"{path.relative_to(ROOT)}:{line_no}: {reference}")

    assert missing == [], "Missing workflow references:\n" + "\n".join(missing)


def _workflow_path_filter_violations(
    workflows_dir: Path, tracked_files: set[str]
) -> list[str]:
    """Return violations for workflow `paths` / `paths-ignore` filters that match no tracked file."""
    violations: list[str] = []
    for wf in sorted(workflows_dir.glob("*.y*ml")):
        data = yaml.safe_load(wf.read_text(encoding="utf-8")) or {}
        on_cfg = data.get("on", data.get(True)) or {}
        if not isinstance(on_cfg, dict):
            continue
        for event in ("push", "pull_request", "pull_request_target"):
            cfg = on_cfg.get(event)
            if not isinstance(cfg, dict):
                continue
            for key in ("paths", "paths-ignore"):
                for pat in cfg.get(key) or []:
                    raw = str(pat).lstrip("!")
                    if any(ch in raw for ch in "*?["):
                        matched = any(
                            PurePosixPath(f).match(raw)
                            or (raw.endswith("/**") and f.startswith(raw[:-2]))
                            for f in tracked_files
                        )
                        if not matched:
                            violations.append(
                                f"{wf.name}:{event}:{key}:{pat} matches no tracked file"
                            )
                    else:
                        clean = raw.rstrip("/")
                        if clean not in tracked_files and not any(
                            f.startswith(clean + "/") for f in tracked_files
                        ):
                            violations.append(
                                f"{wf.name}:{event}:{key}:{pat} does not exist"
                            )
    return violations


def test_workflow_trigger_paths_exist_on_disk() -> None:
    """#1021: every workflow `paths` and `paths-ignore` filter must match at least one tracked file."""
    tracked = set(
        subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).splitlines()
    )
    violations = _workflow_path_filter_violations(
        ROOT / ".github" / "workflows", tracked
    )
    assert violations == [], "Dead workflow path filters:\n" + "\n".join(violations)


def test_workflow_path_filter_guard_rejects_deleted_literal_and_empty_glob(
    tmp_path: Path,
) -> None:
    """#1021: mutation proof for `_workflow_path_filter_violations`."""
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    (wf_dir / "example.yml").write_text(
        "on:\n"
        "  push:\n"
        "    paths:\n"
        "      - 'common/alerting.py'\n"
        "      - 'common/dokploy*.py'\n"
        "      - 'common/alerting/**'\n",
        encoding="utf-8",
    )
    tracked = {"common/alerting/sub/signoz.py"}
    assert _workflow_path_filter_violations(wf_dir, tracked) == [
        "example.yml:push:paths:common/alerting.py does not exist",
        "example.yml:push:paths:common/dokploy*.py matches no tracked file",
    ]
