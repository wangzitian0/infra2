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

    missing = sorted(on_disk - nav_files)
    assert not missing, (
        "Active project docs missing from docs/mkdocs.yml nav (add them under "
        "`- Projects:`):\n" + "\n".join(missing)
    )


def test_mkdocs_nav_lists_every_archived_project_doc() -> None:
    text = MKDOCS.read_text(encoding="utf-8")
    nav_files = set(_NAV_ARCHIVED_RE.findall(text))
    on_disk = _project_doc_names(ARCHIVE_DIR)

    missing = sorted(on_disk - nav_files)
    assert not missing, (
        "Archived project docs missing from docs/mkdocs.yml nav (add them under "
        "`- Archived Projects:`):\n" + "\n".join(missing)
    )
