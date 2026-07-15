from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

WORKFLOW_REFERENCE_RE = re.compile(
    r"(?<![\w-])(?:\.\./)*(?:\./)?\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml"
)
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


def _is_scanned(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    if relative.parts[0] in IGNORED_TOP_LEVEL_DIRS:
        return False
    return path.is_file() and path.suffix in SCANNED_SUFFIXES


def _normalize_workflow_reference(reference: str) -> str:
    return reference[reference.index(".github/workflows/") :]


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
        if not _is_scanned(path):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for match in WORKFLOW_REFERENCE_RE.finditer(content):
            reference = _normalize_workflow_reference(match.group(0))
            if reference not in live_workflows:
                line_no = content.count("\n", 0, match.start()) + 1
                missing.append(f"{path.relative_to(ROOT)}:{line_no}: {reference}")

    assert missing == [], "Missing workflow references:\n" + "\n".join(missing)
