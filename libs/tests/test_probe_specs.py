"""Tests for the runtime probe-spec verification helpers."""

from libs.infra_probes import parse_probe_specs
from libs.probe_specs import (
    missing_probe_names,
    parse_probe_names,
    render_probe_spec_text,
)


def test_otel_and_lark_flow_probes_are_declared():
    """otel 畅通 + lark 畅通 must be AUTOMATED probes: the registry-rendered
    INFRA_PROBE_SPECS (#541: ProbeFacet declarations on the owning Deployers)
    declares the OTLP-ingest collector health and the Feishu delivery
    readiness, distinct from the signoz-query and bridge-process probes."""
    text = render_probe_spec_text()
    names = parse_probe_names(text)
    assert "otel-collector-http" in names, "otel ingest pipeline must be probed"
    assert "lark-delivery-http" in names, "lark delivery readiness must be probed"
    # both are well-formed, critical specs (parse the actual spec lines)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("otel-collector-http|", "lark-delivery-http|")):
            spec = parse_probe_specs(stripped)[0]
            assert spec.kind == "http"
            assert spec.severity == "critical"


def test_synthetic_roundtrip_canaries_are_declared():
    """Observability round-trip closure must be automated probes.

    The 6h real-send ``alert-delivery-canary`` was retired (#425 T3): proving the
    bridge→Feishu path with a periodic *alert* is the anti-pattern #425 forbids. The
    path is now covered by ``lark-delivery-http`` (config + reachability, no real post),
    the out-of-band watchdog's bridge /health check, and the daily reports' delivery.
    """
    text = render_probe_spec_text()
    names = parse_probe_names(text)

    assert "signoz-roundtrip" in names
    assert "openpanel-roundtrip" in names
    # quiet delivery readiness stays (no channel noise); the real-send canary is gone.
    assert "lark-delivery-http" in names
    assert "alert-delivery-canary" not in names

    specs = {
        parse_probe_specs(line.strip())[0].name: parse_probe_specs(line.strip())[0]
        for line in text.splitlines()
        if line.strip().startswith(("signoz-roundtrip|", "openpanel-roundtrip|"))
    }
    assert specs["signoz-roundtrip"].kind == "command"
    assert specs["signoz-roundtrip"].severity == "critical"
    assert specs["openpanel-roundtrip"].kind == "command"
    assert specs["openpanel-roundtrip"].severity == "warning"


def test_parse_probe_names_extracts_first_field():
    specs = (
        "openpanel-api-http|http|http://platform-openpanel-api:3000/healthcheck|200|warning|5\n"
        "platform-postgres-tcp|tcp|platform-postgres:5432|connected|critical|5\n"
    )
    assert parse_probe_names(specs) == {"openpanel-api-http", "platform-postgres-tcp"}


def test_parse_probe_names_ignores_blank_and_malformed_lines():
    assert parse_probe_names("\n   \nno-pipe-here\n  \n") == set()


def test_missing_probe_names_reports_source_not_running():
    source = (
        "a-http|http|x|200|critical|5\n"
        "b-http|http|y|200|critical|5\n"
        "c-http|http|z|200|warning|5\n"
    )
    running = "a-http|http|x|200|critical|5\nb-http|http|y|200|critical|5\n"
    assert missing_probe_names(source, running) == ["c-http"]


def test_missing_probe_names_empty_when_running_is_superset():
    source = "a-http|http|x|200|critical|5\n"
    running = "a-http|http|x|200|critical|5\nb-http|http|y|200|warning|5\n"
    assert missing_probe_names(source, running) == []


def test_missing_probe_names_ignores_target_suffix_differences():
    # The name field (first column) is identical across envs even though the
    # target host carries ${ENV_SUFFIX}; verification keys on names, not targets.
    source = "openpanel-api-http|http|http://platform-openpanel-api:3000/healthcheck|200|warning|5\n"
    running = "openpanel-api-http|http|http://platform-openpanel-api-staging:3000/healthcheck|200|warning|5\n"
    assert missing_probe_names(source, running) == []


def test_render_fails_closed_on_empty_registry_walk():
    """#541 fail-closed layer 1: an empty ProbeFacet walk is never a deployable
    state — the renderer must raise, not ship an empty INFRA_PROBE_SPECS that
    would leave the fleet silently unmonitored."""
    import pytest

    from libs.probe_specs import render_probe_spec_text

    with pytest.raises(ValueError, match="ZERO probes"):
        render_probe_spec_text(attrs={})
