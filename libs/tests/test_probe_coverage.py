"""Coverage audit: every probeable registry service must have an infra probe, or be exempt.

A registry service that declares a service_port is reachable and should have a liveness probe
in the rendered INFRA_PROBE_SPECS — otherwise it is a SILENT monitoring gap (down with no
signal). #541 made both sides registry-derived: probes are ProbeFacet declarations on each
service's Deployer, and an exemption is an explicit `Exemption(check_id="probes", reason=...)`
facet on the SAME Deployer (no more hand-kept exempt dict in this test). A new probeable
platform service with no probe facet and no exemption facet fails CI, so coverage can't
silently regress.
"""

from __future__ import annotations


from libs import service_registry
from libs.probe_specs import render_probe_spec_text


def _probe_spec_rows() -> list[str]:
    """The rendered `name|kind|target|...` rows (registry-derived, no comments)."""
    return [
        line.strip()
        for line in render_probe_spec_text().splitlines()
        if line.strip() and "|" in line
    ]


def _covered_service_ids() -> set[str]:
    """Services with at least one rendered probe, read from the row's own eighth field
    (``service_id``) — the identity the runner reports on, app layer included."""
    covered: set[str] = set()
    for row in _probe_spec_rows():
        fields = [f.strip() for f in row.split("|")]
        if len(fields) >= 8 and fields[7]:
            covered.add(fields[7])
    return covered


def test_probeable_platform_services_have_a_probe_or_are_exempt() -> None:
    covered = _covered_service_ids()
    assert covered, "no probe targets resolved to registry services (renderer drift?)"

    gaps: list[str] = []
    for service_id, meta in service_registry.service_attrs().items():
        if meta.service_port is None:
            continue  # not network-reachable -> nothing to probe
        if service_id in covered or meta.exempted("probes"):
            continue
        gaps.append(service_id)

    assert not gaps, (
        "probeable services with no ProbeFacet (declare probes on the "
        "service's Deployer, or add Exemption(check_id='probes', reason=...)):\n"
        + "\n".join(sorted(gaps))
    )


def test_probe_exemptions_carry_reasons() -> None:
    """An exemption without a written reason is a forgotten gap in disguise."""
    for service_id, meta in service_registry.service_attrs().items():
        for exemption in meta.exemptions:
            assert exemption.reason.strip(), (
                f"{service_id}: Exemption({exemption.check_id!r}) has no reason"
            )
