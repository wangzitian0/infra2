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


def test_compose_carries_only_the_env_reference_after_cutover() -> None:
    """Cutover guard (#541): the compose literal was proven field-identical to
    the frozen fixture (commit history) and then deleted — the renderer is the
    ONLY source now. compose.yaml must reference the env var and never regrow a
    hand-written literal beside it (which the renderer would silently shadow)."""
    import yaml

    compose_text = (ROOT / "platform/12.alerting/compose.yaml").read_text(
        encoding="utf-8"
    )
    compose = yaml.safe_load(compose_text)
    value = compose["services"]["infra-probe-runner"]["environment"][
        "INFRA_PROBE_SPECS"
    ]
    assert value == "${INFRA_PROBE_SPECS:-}", (
        "platform/12.alerting/compose.yaml INFRA_PROBE_SPECS must stay the "
        "${INFRA_PROBE_SPECS:-} reference — probes are declared as ProbeFacets "
        f"on their owning Deployers, not in compose. Got: {value[:120]!r}"
    )
    assert "dokploy-internal-http|" not in compose_text  # no literal resurrection


def test_alerting_env_base_ships_the_rendered_specs(monkeypatch) -> None:
    """The deploy vehicle end to end: AlertingDeployer.compose_env_base (the
    hook the iac-runner sync path actually uses — NOT pre_compose) and the
    secret-free source_config_env_base both carry the encoded rendered specs."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "alerting_deploy", ROOT / "platform/12.alerting/deploy.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    deployer = module.AlertingDeployer

    class _NoSecrets:
        def get(self, key):
            return None

    monkeypatch.setattr(module, "get_secrets", lambda **kw: _NoSecrets())
    env = {
        "ENV": "staging",
        "ENV_SUFFIX": "-staging",
        "INTERNAL_DOMAIN": "example.test",
        "ENV_DOMAIN_SUFFIX": "staging",
        "DATA_PATH": "/data/platform/alerting-staging",
    }
    expected = encode_specs_env_value(
        resolve_env_suffix(render_probe_spec_text(), "-staging")
    )
    assert deployer.compose_env_base(env)["INFRA_PROBE_SPECS"] == expected
    assert deployer.source_config_env_base(env)["INFRA_PROBE_SPECS"] == expected
    # decoding what shipped yields the exact probe set the registry declares
    shipped = normalize_specs_text(expected)
    assert parse_probe_names(shipped) == parse_probe_names(render_probe_spec_text())


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


def test_verify_runtime_applied_fails_closed_on_empty_deployed_specs(monkeypatch):
    """#541 fail-closed layer 2: an empty deployed INFRA_PROBE_SPECS means the
    renderer's output never reached the deploy env (the renderer itself raises
    on an empty walk, so this can only be a transport drop). verify must return
    an error string — never skip as 'nothing to verify'."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "alerting_deploy_verify", ROOT / "platform/12.alerting/deploy.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    deployer = module.AlertingDeployer
    monkeypatch.setattr(deployer, "env", classmethod(lambda cls: {"VPS_HOST": "x"}))

    err = deployer.verify_runtime_applied(None, {"INFRA_PROBE_SPECS": ""})
    assert err is not None and "blind" in err
