"""#541 probe migration equivalence proof — PERMANENT regression protection.

``libs/tests/fixtures/infra_probe_specs_frozen.txt`` is a byte-copy of the
hand-written ``INFRA_PROBE_SPECS`` compose literal as it stood before the facet
migration (comments and all). The registry-rendered specs (every service's
ProbeFacet declarations -> ``service_attrs()`` ->
``render_probe_spec_text()``) must stay equivalent to it FIELD BY FIELD,
order-normalized — not merely name-equal. This is not a one-time diff: the
frozen fixture anchors the probe set forever, so a facet edit that
accidentally drops or mutates a probe fails here with an exact field diff.

Deliberate probe-set changes must update the fixture in the same PR, making
the change explicit and reviewable (the fixture is the probe changelog).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from libs import service_registry
from libs.probe_specs import (
    ENV_SUFFIX_PLACEHOLDER,
    encode_specs_env_value,
    missing_probe_names,
    normalize_specs_text,
    normalized_probe_fields,
    parse_probe_names,
    render_probe_spec_text,
    resolve_env_suffix,
)

ROOT = Path(__file__).resolve().parents[2]
FROZEN = ROOT / "libs/tests/fixtures/infra_probe_specs_frozen.txt"


def _frozen_text() -> str:
    return FROZEN.read_text(encoding="utf-8")


def test_rendered_specs_equal_frozen_literal_field_by_field() -> None:
    """THE equivalence proof: generated == frozen, per probe, per field."""
    generated = normalized_probe_fields(render_probe_spec_text())
    frozen = normalized_probe_fields(_frozen_text())

    generated_by_name = {row[0]: row for row in generated}
    frozen_by_name = {row[0]: row for row in frozen}
    assert sorted(generated_by_name) == sorted(frozen_by_name), (
        "probe NAME set diverged from the frozen literal.\n"
        f"missing from generated: {sorted(set(frozen_by_name) - set(generated_by_name))}\n"
        f"new in generated:       {sorted(set(generated_by_name) - set(frozen_by_name))}\n"
        "If this change is deliberate, update libs/tests/fixtures/"
        "infra_probe_specs_frozen.txt in the same PR."
    )
    for name, frozen_row in frozen_by_name.items():
        assert generated_by_name[name] == frozen_row, (
            f"probe {name!r} diverged from the frozen literal, field by field:\n"
            f"  frozen:    {frozen_row}\n"
            f"  generated: {generated_by_name[name]}"
        )


def test_rendered_specs_name_equivalence_via_live_check_helper() -> None:
    """The live-verification bridge (missing_probe_names) sees the same probe
    set in both directions between generated and frozen."""
    generated = render_probe_spec_text()
    frozen = _frozen_text()
    assert missing_probe_names(frozen, generated) == []
    assert missing_probe_names(generated, frozen) == []
    assert parse_probe_names(generated) == parse_probe_names(frozen)
    assert len(parse_probe_names(generated)) == 21  # the frozen probe count


def test_frozen_fixture_matches_live_compose_literal() -> None:
    """While the compose literal is still the live source (migration phase),
    the frozen fixture must be its exact byte copy — proving the fixture froze
    the real thing. This test is REMOVED at cutover (literal deleted)."""
    import yaml

    compose = yaml.safe_load(
        (ROOT / "platform/12.alerting/compose.yaml").read_text(encoding="utf-8")
    )
    literal = compose["services"]["infra-probe-runner"]["environment"][
        "INFRA_PROBE_SPECS"
    ]
    assert literal == _frozen_text()


def test_rendering_is_deterministic() -> None:
    assert render_probe_spec_text() == render_probe_spec_text()


def test_duplicate_probe_names_across_services_fail_closed() -> None:
    attrs = service_registry.service_attrs()
    dup = dict(attrs)
    donor = dup["platform/minio"]
    from dataclasses import replace

    dup["platform/zz-dup"] = replace(
        donor, service_id="platform/zz-dup", probes=donor.probes
    )
    with pytest.raises(ValueError, match="duplicate ProbeFacet name"):
        render_probe_spec_text(dup)


# --- env-suffix semantics (the #161 isolation rules, registry-derived) -------


def test_env_suffix_placeholders_resolve_for_each_env() -> None:
    text = render_probe_spec_text()
    staging = resolve_env_suffix(text, "-staging")
    prod = resolve_env_suffix(text, "")
    assert ENV_SUFFIX_PLACEHOLDER not in staging
    assert ENV_SUFFIX_PLACEHOLDER not in prod
    assert "platform-postgres-staging:5432" in staging
    assert "platform-postgres:5432" in prod
    # prod_only shared instances are NEVER suffixed, in any env
    assert "http://platform-signoz:8080" in staging
    assert "platform-openpanel-api:3000" in staging


def test_env_value_encoding_round_trips_and_fails_closed() -> None:
    text = resolve_env_suffix(render_probe_spec_text(), "-staging")
    encoded = encode_specs_env_value(text)
    assert "\n" not in encoded  # single dotenv line
    assert encoded.startswith('"') and encoded.endswith('"')
    # tolerant decode: expanded, still-encoded, and quoted forms all normalize
    assert normalize_specs_text(encoded) == text
    assert normalize_specs_text(text) == text
    assert parse_probe_names(encoded) == parse_probe_names(text)
    # an unresolved placeholder (or any $) must never reach the env transport
    with pytest.raises(ValueError, match="dotenv"):
        encode_specs_env_value(render_probe_spec_text())


# --- comment intent preserved as mechanism, not prose ------------------------


def test_probe_env_suffix_agrees_with_registry_prod_only() -> None:
    """The old literal documented 'prod_only => no suffix' as comments; the
    facet form enforces it from the registry fact, for the rendered output."""
    problems: list[str] = []
    for row in normalized_probe_fields(render_probe_spec_text()):
        name, kind, target = row[0], row[1], row[2]
        if kind in ("command", "resource"):
            continue
        netloc = target.split("://", 1)[-1].split("/", 1)[0]
        host = netloc.split(":", 1)[0]
        meta = service_registry.resolve_container_host(host)
        if meta is None:
            continue  # bootstrap plane — outside the registry scan
        has_suffix = ENV_SUFFIX_PLACEHOLDER in host
        if meta.prod_only and has_suffix:
            problems.append(f"{name}: prod_only service must NOT use ENV_SUFFIX")
        if not meta.prod_only and not has_suffix:
            problems.append(f"{name}: per-env service must carry ENV_SUFFIX")
    assert not problems, "\n".join(problems)


def test_rendered_targets_pass_env_isolation_lint() -> None:
    """The #161 env-isolation lint used to catch bare per-env hosts in
    compose.yaml; apply the same regex to the rendered spec text so the
    protection follows the probes into their facet form."""
    from libs.tests.test_env_isolation import _HOST_REF

    violations = [
        line for line in render_probe_spec_text().splitlines() if _HOST_REF.search(line)
    ]
    assert not violations, (
        "rendered probe target(s) reference a per-env service without "
        "${ENV_SUFFIX}:\n" + "\n".join(violations)
    )


def test_out_of_registry_probes_carry_explicit_service_ids() -> None:
    """Bootstrap/host probes are declared on the alerting Deployer with
    explicit service_ids; every rendered service_id is either a registry id or
    one of the known out-of-registry component ids."""
    attrs = service_registry.service_attrs()
    known_external = set(service_registry._EXTERNAL_COMPONENT_IDS.values())
    for row in normalized_probe_fields(render_probe_spec_text()):
        service_id = row[7]
        assert service_id, f"probe {row[0]} rendered without a service_id"
        assert service_id in attrs or service_id in known_external, (
            f"probe {row[0]} carries unknown service_id {service_id!r}"
        )
