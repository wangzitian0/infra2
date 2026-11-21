"""Project lifecycle governance tests.

Guards against premature archival and documentation drift:
- Active runtime services (e.g. truealpha/data_engine - Dagster) MUST NOT have
  their project closed in the archive index while the service is still active.
- Closed projects are entries of one provenance index,
  docs/project/archive/README.md (#1826). The archive holds no other file, each
  entry has a closed status, an outcome and links, and the index stays small.
"""

from __future__ import annotations

import re
from pathlib import Path

from common.core import registry as service_registry

ROOT = Path(__file__).resolve().parents[2]
PROJECT_DIR = ROOT / "docs/project"
ARCHIVE_DIR = PROJECT_DIR / "archive"
ARCHIVE_INDEX = ARCHIVE_DIR / "README.md"
#: The size budget of the archive index (#1826): 25 KiB.
ARCHIVE_INDEX_MAX_BYTES = 25 * 1024

_ENTRY_RE = re.compile(r"^### (Infra-\d+): .+$", re.MULTILINE)
_FIELD_RE = r"^- \*\*{name}\*\*: (.+)$"

# Map of service_id -> expected active project doc (if managed under project RFC)
ACTIVE_SERVICE_PROJECT_DOCS = {
    "truealpha/data_engine": "Infra-017.truealpha_dagster_capture.md",
}


def test_active_services_not_in_project_archive() -> None:
    """Ensure active services do not have their defining project doc archived."""
    active_services = service_registry.service_attrs()

    for service_id, doc_name in ACTIVE_SERVICE_PROJECT_DOCS.items():
        assert service_id in active_services, (
            f"Service {service_id} is expected to be registered in service_registry"
        )
        project_id = doc_name.split(".", 1)[0]
        assert project_id not in _archive_entries(), (
            f"Drift detected: {project_id} is closed in {ARCHIVE_INDEX.name} but "
            f"service '{service_id}' is actively registered in service_registry!"
        )
        active_doc = PROJECT_DIR / doc_name
        assert active_doc.exists(), (
            f"Active doc {active_doc} must exist in docs/project/"
        )

        # Verify status is Active
        content = active_doc.read_text(encoding="utf-8")
        status_match = re.search(
            r"^\s*>?\s*\*\*(?:Status|状态)\*\*:?\s*(.+?)\s*$", content, re.MULTILINE
        )
        assert status_match is not None, f"{doc_name} missing Status header"
        status_val = status_match.group(1).strip()
        assert "Active" in status_val or "In Progress" in status_val, (
            f"{doc_name} documents active service {service_id}, but status is: '{status_val}'"
        )


def _archive_entries() -> dict[str, str]:
    """Map each `### Infra-NNN: Title` entry of the archive index to its body."""
    text = ARCHIVE_INDEX.read_text(encoding="utf-8")
    matches = list(_ENTRY_RE.finditer(text))
    entries: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end].split("\n## ", 1)[0]
        assert match.group(1) not in entries, f"duplicate entry {match.group(1)}"
        entries[match.group(1)] = body
    return entries


def _field(body: str, name: str) -> str | None:
    match = re.search(_FIELD_RE.format(name=name), body, re.MULTILINE)
    return match.group(1).strip() if match else None


def test_archive_holds_only_the_index() -> None:
    """A closed project is an index entry, not a file (#1826)."""
    assert ARCHIVE_INDEX.is_file(), f"missing {ARCHIVE_INDEX.relative_to(ROOT)}"
    others = sorted(
        p.name for p in ARCHIVE_DIR.iterdir() if p.name != ARCHIVE_INDEX.name
    )
    assert not others, (
        f"docs/project/archive/ must hold only {ARCHIVE_INDEX.name}. Add an entry "
        f"to the index and delete these files: {others}"
    )


def test_archive_index_stays_within_its_size_budget() -> None:
    size = ARCHIVE_INDEX.stat().st_size
    assert size <= ARCHIVE_INDEX_MAX_BYTES, (
        f"{ARCHIVE_INDEX.relative_to(ROOT)} is {size} bytes; the budget is "
        f"{ARCHIVE_INDEX_MAX_BYTES} bytes. Shorten the entries."
    )


def test_archive_entries_are_closed_and_traceable() -> None:
    """Each entry has a closed Status, an Outcome and a Links line."""
    entries = _archive_entries()
    assert len(entries) >= 16, f"the index has only {len(entries)} entries"
    for project_id, body in entries.items():
        status = _field(body, "Status")
        assert status is not None, f"{project_id} has no Status line"
        assert status.startswith("Archived"), (
            f"{project_id} has Status: '{status}'. An archive entry must have "
            "Status: Archived."
        )
        assert _field(body, "Outcome"), f"{project_id} has no Outcome line"
        links = _field(body, "Links")
        assert links, f"{project_id} has no Links line"
        assert "https://github.com/" in links or "no PR recorded" in links, (
            f"{project_id} Links line has no GitHub link and does not say "
            "'no PR recorded'"
        )


def test_no_active_project_doc_is_also_an_archive_entry() -> None:
    active = {p.name.split(".", 1)[0] for p in PROJECT_DIR.glob("Infra-*.md")}
    both = sorted(active & set(_archive_entries()))
    assert not both, f"projects both active and archived: {both}"
