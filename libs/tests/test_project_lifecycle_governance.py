"""Project lifecycle governance tests.

Guards against premature archival and documentation drift:
- Active runtime services (e.g. truealpha/data_engine - Dagster) MUST NOT have
  their project docs buried in docs/project/archive/ while the service is still active.
- Bidirectional consistency: Archived project docs must not document active production services.
"""

from __future__ import annotations

import re
from pathlib import Path

from libs import service_registry

ROOT = Path(__file__).resolve().parents[2]
PROJECT_DIR = ROOT / "docs/project"
ARCHIVE_DIR = PROJECT_DIR / "archive"

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
        archive_doc = ARCHIVE_DIR / doc_name
        assert not archive_doc.exists(), (
            f"Drift detected: {doc_name} is in docs/project/archive/ but service "
            f"'{service_id}' is actively registered in service_registry!"
        )
        active_doc = PROJECT_DIR / doc_name
        assert active_doc.exists(), f"Active doc {active_doc} must exist in docs/project/"
        
        # Verify status is Active
        content = active_doc.read_text(encoding="utf-8")
        status_match = re.search(r"^\s*>?\s*\*\*(?:Status|状态)\*\*:?\s*(.+?)\s*$", content, re.MULTILINE)
        assert status_match is not None, f"{doc_name} missing Status header"
        status_val = status_match.group(1).strip()
        assert "Active" in status_val or "In Progress" in status_val, (
            f"{doc_name} documents active service {service_id}, but status is: '{status_val}'"
        )


def test_archived_docs_do_not_claim_active_status() -> None:
    """Archived docs in docs/project/archive/ must not declare Status: Active."""
    for archive_file in ARCHIVE_DIR.glob("Infra-*.md"):
        content = archive_file.read_text(encoding="utf-8")
        status_match = re.search(r"^\s*>?\s*\*\*(?:Status|状态)\*\*:?\s*(.+?)\s*$", content, re.MULTILINE)
        if status_match:
            status_val = status_match.group(1).strip()
            assert not status_val.startswith("Active"), (
                f"Archived file {archive_file.name} has Status: '{status_val}'. "
                "Archived docs must have Status: Archived / Completed / Closed."
            )
