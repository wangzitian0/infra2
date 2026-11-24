#!/usr/bin/env python3
"""Generate the Active Projects section in docs/project/README.md from each
active Infra-XXX doc's own H1 title and Status header.

The portfolio previously hand-copied each project's status into the index —
Infra-004 and Infra-005 drifted to "In Progress" in the index while their own
docs said "Completed", and Infra-010/014/016 were missing entirely (#505).
This makes the index a pure projection of the docs directory, so it cannot
drift from what each doc actually says. `test_generated_index_equals_
committed` locks generated == committed; run `python tools/gen_project_index.py
--write` to regenerate after adding/renaming/re-statusing a project doc.

Closed projects have no file of their own. Each one is an entry in the
provenance index docs/project/archive/README.md (#1826), and
common/tests/test_project_lifecycle_governance.py guards that index. A doc
whose Status says "Archived" but stays in docs/project/ is a real, visible
drift: this generator lists it as active and does not hide it.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_DIR = ROOT / "docs/project"
README = PROJECT_DIR / "README.md"

BEGIN_ACTIVE = "<!-- BEGIN GENERATED ACTIVE PROJECTS (tools/gen_project_index.py) -->"
END_ACTIVE = "<!-- END GENERATED ACTIVE PROJECTS -->"

_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_STATUS_RE = re.compile(r"^\s*>?\s*\*\*(?:Status|状态)\*\*:?\s*(.+?)\s*$", re.MULTILINE)
_SKIP_SUFFIXES = (".TODOWRITE.md", ".SUMMARY.md")


_NUMBER_RE = re.compile(r"^Infra-(\d+)")


def _project_number(path: Path) -> int:
    """The Infra id as a number, for ordering.

    Sorting these paths as strings is only accidentally right: it agrees with
    numeric order while every id has the same width, and stops the moment one
    does not. "Infra-999" sorts above "Infra-1000" as text, so the newest doc
    would land below an older one under a comment promising the opposite.
    Dormant today — the ids are in the twenties — and silent when it arrives.
    """
    match = _NUMBER_RE.match(path.name)
    if match is None:
        # SystemExit, not assert: `python -O` strips assertions, which would
        # turn this into an AttributeError on `None.group` and lose the message.
        # Matches how the rest of this script reports a refusal.
        raise SystemExit(
            f"gen_project_index: {path.name} does not start with an Infra id"
        )
    return int(match.group(1))


def _project_docs(directory: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in directory.glob("Infra-*.md")
            if not path.name.endswith(_SKIP_SUFFIXES)
        ),
        # newest (highest Infra-NNN) first, matching the prior hand order
        key=_project_number,
        reverse=True,
    )


def _extract(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    title_match = _TITLE_RE.search(text)
    status_match = _STATUS_RE.search(text)
    if title_match is None:
        raise SystemExit(f"gen_project_index: no H1 title in {path.relative_to(ROOT)}")
    if status_match is None:
        raise SystemExit(
            f"gen_project_index: no Status line in {path.relative_to(ROOT)}"
        )
    return title_match.group(1).strip(), status_match.group(1).strip()


def render_section(directory: Path, link_prefix: str) -> str:
    rows = []
    for path in _project_docs(directory):
        title, status = _extract(path)
        rows.append(f"- [{title}]({link_prefix}{path.name}) - **{status}**")
    return "\n".join(rows)


def render_readme() -> str:
    text = README.read_text(encoding="utf-8")
    if BEGIN_ACTIVE not in text or END_ACTIVE not in text:
        raise SystemExit(
            f"gen_project_index: markers not found in {README} — add them once"
        )

    text = (
        text.split(BEGIN_ACTIVE)[0]
        + BEGIN_ACTIVE
        + "\n\n"
        + render_section(PROJECT_DIR, "./")
        + "\n\n"
        + END_ACTIVE
        + text.split(END_ACTIVE, 1)[1]
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
        print(
            "gen_project_index: docs/project/README.md is stale — run with --write",
            file=sys.stderr,
        )
        return 1
    print("docs/project/README.md is up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
