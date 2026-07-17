#!/usr/bin/env python3
"""Generate the Active/Archived Projects sections in docs/project/README.md from
each Infra-XXX doc's own H1 title and Status header.

The portfolio previously hand-copied each project's status into the index —
Infra-004 and Infra-005 drifted to "In Progress" in the index while their own
docs said "Completed", and Infra-010/011/014/016 were missing entirely (#505).
This makes the index a pure projection of the docs directory, so it cannot
drift from what each doc actually says. `test_project_index_generated_matches_
committed` locks generated == committed; run `python tools/gen_project_index.py
--write` to regenerate after adding/renaming/re-statusing a project doc.

Archived vs. active is which directory the file lives in (docs/project/ vs.
docs/project/archive/), not string-matching the status text — a doc whose
Status says "Archived" but hasn't been moved yet is a real, visible drift this
generator will NOT paper over (it stays in whichever section its directory
says, so its status text and its section can visibly disagree — that's the
day's actual state, not something to silently reconcile).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_DIR = ROOT / "docs/project"
ARCHIVE_DIR = PROJECT_DIR / "archive"
README = PROJECT_DIR / "README.md"

BEGIN_ACTIVE = "<!-- BEGIN GENERATED ACTIVE PROJECTS (tools/gen_project_index.py) -->"
END_ACTIVE = "<!-- END GENERATED ACTIVE PROJECTS -->"
BEGIN_ARCHIVED = "<!-- BEGIN GENERATED ARCHIVED PROJECTS (tools/gen_project_index.py) -->"
END_ARCHIVED = "<!-- END GENERATED ARCHIVED PROJECTS -->"

_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_STATUS_RE = re.compile(r"^\s*>?\s*\*\*(?:Status|状态)\*\*:?\s*(.+?)\s*$", re.MULTILINE)
_SKIP_SUFFIXES = (".TODOWRITE.md", ".SUMMARY.md")


def _project_docs(directory: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in directory.glob("Infra-*.md")
            if not path.name.endswith(_SKIP_SUFFIXES)
        ),
        reverse=True,  # newest (highest Infra-NNN) first, matching the prior hand order
    )


def _extract(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    title_match = _TITLE_RE.search(text)
    status_match = _STATUS_RE.search(text)
    if title_match is None:
        raise SystemExit(f"gen_project_index: no H1 title in {path.relative_to(ROOT)}")
    if status_match is None:
        raise SystemExit(f"gen_project_index: no Status line in {path.relative_to(ROOT)}")
    return title_match.group(1).strip(), status_match.group(1).strip()


def render_section(directory: Path, link_prefix: str) -> str:
    rows = []
    for path in _project_docs(directory):
        title, status = _extract(path)
        rows.append(f"- [{title}]({link_prefix}{path.name}) - **{status}**")
    return "\n".join(rows)


def render_readme() -> str:
    text = README.read_text(encoding="utf-8")
    for begin, end in ((BEGIN_ACTIVE, END_ACTIVE), (BEGIN_ARCHIVED, END_ARCHIVED)):
        if begin not in text or end not in text:
            raise SystemExit(f"gen_project_index: markers not found in {README} — add them once")

    text = (
        text.split(BEGIN_ACTIVE)[0]
        + BEGIN_ACTIVE
        + "\n\n"
        + render_section(PROJECT_DIR, "./")
        + "\n\n"
        + END_ACTIVE
        + text.split(END_ACTIVE, 1)[1]
    )
    text = (
        text.split(BEGIN_ARCHIVED)[0]
        + BEGIN_ARCHIVED
        + "\n\n"
        + render_section(ARCHIVE_DIR, "./archive/")
        + "\n\n"
        + END_ARCHIVED
        + text.split(END_ARCHIVED, 1)[1]
    )
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite README in place")
    args = parser.parse_args()

    expected = render_readme()
    if args.write:
        README.write_text(expected, encoding="utf-8")
        print(f"wrote {README}")
        return 0

    actual = README.read_text(encoding="utf-8")
    if actual != expected:
        print("gen_project_index: docs/project/README.md is stale — run with --write", file=sys.stderr)
        return 1
    print("docs/project/README.md is up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
