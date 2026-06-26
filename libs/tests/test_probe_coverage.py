"""Coverage audit: every probeable platform service must have an infra probe, or be exempt.

A registry service that declares a service_port is reachable and should have a liveness probe
in INFRA_PROBE_SPECS — otherwise it is a SILENT monitoring gap (down with no signal). This
binds that to the registry: a new probeable platform service with no probe and no explicit
exemption fails CI, so coverage can't silently regress.

Exemptions are explicit and justified — anything here is a conscious "not probed here", not a
forgotten gap.
"""

from __future__ import annotations

import re
from pathlib import Path

from libs import service_registry

ROOT = Path(__file__).resolve().parents[2]
SPECS = ROOT / "platform/12.alerting/compose.yaml"

# service_id -> reason it has no INFRA_PROBE_SPECS entry. Keep justified.
_PROBE_EXEMPT = {
    "platform/portal": "static homer dashboard — no liveness endpoint to probe",
}


def _probe_spec_rows() -> list[str]:
    """The `name|kind|target|...` rows of the INFRA_PROBE_SPECS block-scalar ONLY.

    Bounded by the block's indentation — must NOT scan the rest of the compose file, or
    `platform-*` in later container_name:/env fields would count as false coverage and mask a
    genuinely unprobed service (Copilot CR).
    """
    lines = SPECS.read_text(encoding="utf-8").splitlines()
    rows: list[str] = []
    grabbing = False
    block_indent = 0
    for line in lines:
        if not grabbing:
            if "INFRA_PROBE_SPECS:" in line and line.rstrip().endswith("|"):
                grabbing = True
                block_indent = len(line) - len(line.lstrip())
            continue
        if line.strip() == "":
            continue
        if len(line) - len(line.lstrip()) <= block_indent:
            break  # dedented out of the block scalar
        row = line.strip()
        if not row.startswith("#") and "|" in row:
            rows.append(row)
    return rows


def _covered_service_ids() -> set[str]:
    covered: set[str] = set()
    for row in _probe_spec_rows():
        for host in re.findall(r"platform-[\w-]+", row):
            meta = service_registry.resolve_container_host(host)
            if meta is not None:
                covered.add(meta.service_id)
    return covered


def test_probeable_platform_services_have_a_probe_or_are_exempt() -> None:
    covered = _covered_service_ids()
    assert covered, "no probe targets resolved to registry services (parser drift?)"

    gaps: list[str] = []
    for service_id, meta in service_registry.service_attrs().items():
        if meta.service_port is None:
            continue  # not network-reachable -> nothing to probe
        if meta.layer != "platform":
            continue  # app layer (finance_report) is app-monitored, not infra-probed
        if service_id in covered or service_id in _PROBE_EXEMPT:
            continue
        gaps.append(service_id)

    assert not gaps, (
        "probeable platform services with no INFRA_PROBE_SPECS probe (add a probe, or add to "
        "_PROBE_EXEMPT with a reason):\n" + "\n".join(sorted(gaps))
    )
