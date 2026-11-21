"""mkdocs nav must list every project doc — like the SSOT nav guard
(test_mkdocs_nav_manifest.py), the Projects/Archived Projects nav sections are
hand-curated (labels don't come from the doc's own title), so they drift
silently otherwise. Before this guard, nav had 7 of 17 active projects and was
missing Infra-018 from Archived (#505). We enforce COMPLETENESS (nav ⊇ docs on
disk), not generation, for the same reason as the SSOT nav: a mis-generated
nested YAML would break the docs build.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MKDOCS = ROOT / "docs/mkdocs.yml"
PROJECT_DIR = ROOT / "docs/project"
ARCHIVE_DIR = PROJECT_DIR / "archive"

_SKIP_SUFFIXES = (".TODOWRITE.md", ".SUMMARY.md")
_NAV_ACTIVE_RE = re.compile(r"project/(Infra-[\w.-]+\.md)")
_NAV_ARCHIVED_RE = re.compile(r"project/archive/(Infra-[\w.-]+\.md)")
# An uncommented nav entry: a YAML comment line or a trailing comment cannot match.
_NAV_ARCHIVE_INDEX_RE = re.compile(
    r"^\s*- Archived Projects: project/archive/README\.md\s*$", re.MULTILINE
)


def _project_doc_names(directory: Path) -> set[str]:
    return {
        path.name
        for path in directory.glob("Infra-*.md")
        if not path.name.endswith(_SKIP_SUFFIXES)
    }


def test_mkdocs_nav_lists_every_active_project_doc() -> None:
    text = MKDOCS.read_text(encoding="utf-8")
    nav_files = set(_NAV_ACTIVE_RE.findall(text)) - set(_NAV_ARCHIVED_RE.findall(text))
    on_disk = _project_doc_names(PROJECT_DIR)

    assert len(on_disk) > 0, f"No active project docs discovered in {PROJECT_DIR}"
    assert len(nav_files) > 0, "No active nav files parsed from mkdocs.yml"
    missing = sorted(on_disk - nav_files)
    assert not missing, (
        "Active project docs missing from docs/mkdocs.yml nav (add them under "
        "`- Projects:`):\n" + "\n".join(missing)
    )


def test_mkdocs_nav_lists_the_archive_index() -> None:
    """Closed projects have one page, the provenance index (#1826). The nav
    must list it, and must list no per-project archive page."""
    text = MKDOCS.read_text(encoding="utf-8")
    assert (ARCHIVE_DIR / "README.md").is_file(), (
        "missing docs/project/archive/README.md"
    )
    assert _NAV_ARCHIVE_INDEX_RE.search(text), (
        "docs/mkdocs.yml nav does not list project/archive/README.md under "
        "`- Archived Projects:`"
    )
    stale = sorted(set(_NAV_ARCHIVED_RE.findall(text)))
    assert not stale, (
        "docs/mkdocs.yml nav lists per-project archive pages; closed projects "
        "are entries of project/archive/README.md:\n" + "\n".join(stale)
    )
