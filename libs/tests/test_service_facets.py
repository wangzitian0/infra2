"""#541 convergence part 1: facet declarations on Deployer classes, the AST
facet reader in the service registry, and the service × facet completeness
matrix (including the Infra-012.10 counterfactual the matrix must flag)."""

from __future__ import annotations

import ast

import pytest

from libs import service_registry as reg
from libs.service_facets import BackupFacet, Exemption, ProbeFacet, SignalFacet
from tools.service_facet_matrix import (
    build_matrix,
    cell_state,
    consistency_flags,
    main as matrix_main,
    render_report,
)


def _meta(service_id: str = "platform/example", **overrides) -> reg.ServiceMeta:
    base = dict(
        service_id=service_id,
        layer=service_id.split("/", 1)[0],
        service=service_id.split("/", 1)[1],
        prod_only=False,
        subdomain=None,
        service_port=None,
        service_name=None,
        telemetry_service_name=None,
        telemetry_component=None,
        project="platform",
    )
    base.update(overrides)
    return reg.ServiceMeta(**base)


# --- AST facet reader --------------------------------------------------------


_DEPLOY_SOURCE = """
from libs.deploy.deployer import Deployer, make_tasks
from libs.service_facets import BackupFacet, Exemption, ProbeFacet, SignalFacet


class ExampleDeployer(Deployer):
    service = "example"
    probes = (
        ProbeFacet(
            name="example-http",
            kind="http",
            target="http://platform-example${ENV_SUFFIX}:8080/health",
            expected="200",
        ),
        ProbeFacet(
            name="example-roundtrip",
            kind="command",
            target="python /app/tools/x.py example",
            expected="roundtrip-ok",
            severity="warning",
            timeout_seconds=45,
            depends_on="example-http",
        ),
    )
    signals = (
        SignalFacet(
            tier="minute", type="alert", consecutive_failures=3,
            renotify_window_sec=1800,
        ),
    )
    backups = (BackupFacet(method="pg_dump", retention_days=30, rpo_hours=24),)
    exemptions = (Exemption(check_id="probes", reason="covered elsewhere"),)
"""


def test_facet_reader_extracts_literal_declarations() -> None:
    tree = ast.parse(_DEPLOY_SOURCE)
    probes = reg._facet_seq(tree, "probes", ProbeFacet, "example/deploy.py")
    assert probes == (
        ProbeFacet(
            name="example-http",
            kind="http",
            target="http://platform-example${ENV_SUFFIX}:8080/health",
            expected="200",
        ),
        ProbeFacet(
            name="example-roundtrip",
            kind="command",
            target="python /app/tools/x.py example",
            expected="roundtrip-ok",
            severity="warning",
            timeout_seconds=45,
            depends_on="example-http",
        ),
    )
    signals = reg._facet_seq(tree, "signals", SignalFacet, "example/deploy.py")
    assert signals[0].tier == "minute"
    assert signals[0].consecutive_failures == 3
    backups = reg._facet_seq(tree, "backups", BackupFacet, "example/deploy.py")
    assert backups == (BackupFacet(method="pg_dump", retention_days=30, rpo_hours=24),)
    exemptions = reg._facet_seq(tree, "exemptions", Exemption, "example/deploy.py")
    assert exemptions[0].check_id == "probes"


def test_facet_reader_defaults_when_absent() -> None:
    tree = ast.parse("class XDeployer(Deployer):\n    service = 'x'\n")
    assert reg._facet_seq(tree, "probes", ProbeFacet, "x") == ()
    assert reg._facet_seq(tree, "backups", BackupFacet, "x") == ()


def test_facet_reader_ignores_nested_and_helper_classes() -> None:
    source = (
        "class Helper:\n"
        "    probes = (ProbeFacet(name='shadow', kind='http', target='t'),)\n"
        "class XDeployer(Deployer):\n"
        "    service = 'x'\n"
    )
    tree = ast.parse(source)
    assert reg._facet_seq(tree, "probes", ProbeFacet, "x") == ()


def test_facet_reader_fails_closed_on_non_literal_arguments() -> None:
    """A facet the reader cannot evaluate must raise, never silently drop —
    a dropped ProbeFacet would silently vanish from the rendered specs."""
    source = (
        "class XDeployer(Deployer):\n"
        "    probes = (ProbeFacet(name=NAME, kind='http', target='t'),)\n"
    )
    with pytest.raises(ValueError, match="literal"):
        reg._facet_seq(ast.parse(source), "probes", ProbeFacet, "x/deploy.py")


def test_facet_reader_fails_closed_on_wrong_constructor_or_shape() -> None:
    wrong_ctor = "class XDeployer(Deployer):\n    probes = (SignalFacet(tier='minute', type='alert'),)\n"
    with pytest.raises(ValueError, match="ProbeFacet"):
        reg._facet_seq(ast.parse(wrong_ctor), "probes", ProbeFacet, "x/deploy.py")
    not_a_seq = "class XDeployer(Deployer):\n    probes = 'oops'\n"
    with pytest.raises(ValueError, match="tuple/list"):
        reg._facet_seq(ast.parse(not_a_seq), "probes", ProbeFacet, "x/deploy.py")


def test_probe_facet_spec_line_matches_probe_specs_format() -> None:
    """spec_line renders the exact 8-field `name|kind|target|expected|severity|
    timeout|depends_on|service_id` line libs.infra_probes.parse_probe_specs reads."""
    from libs.infra_probes import parse_probe_specs

    facet = ProbeFacet(
        name="minio-internal-http",
        kind="http",
        target="http://platform-minio:9000/minio/health/live",
        expected="200",
    )
    line = facet.spec_line(default_service_id="platform/minio")
    assert line == (
        "minio-internal-http|http|http://platform-minio:9000/minio/health/live"
        "|200|critical|5||platform/minio"
    )
    spec = parse_probe_specs(line)[0]
    assert spec.service_id == "platform/minio"
    assert spec.timeout_seconds == 5.0
    # an explicit service_id (out-of-registry owner) wins over the declarer's id
    external = ProbeFacet(
        name="vault-internal-http",
        kind="http",
        target="http://vault:8200/v1/sys/health",
        expected="200,429,472,473",
        service_id="bootstrap/vault",
    )
    assert external.spec_line("platform/alerting").endswith("|bootstrap/vault")


# --- registry exposure (skeleton must be a zero-behavior change) -------------


def test_service_attrs_exposes_facet_fields_for_every_service() -> None:
    for meta in reg.service_attrs().values():
        assert isinstance(meta.probes, tuple)
        assert isinstance(meta.signals, tuple)
        assert isinstance(meta.exemptions, tuple)
        assert all(isinstance(b, BackupFacet) for b in meta.backups)
        assert isinstance(meta.deploy_v2_canary, bool)


def test_deployer_base_facet_defaults_are_empty() -> None:
    from libs.deploy.deployer import Deployer

    assert Deployer.probes == ()
    assert Deployer.signals == ()
    assert Deployer.backups == ()
    assert Deployer.exemptions == ()
    assert Deployer.deploy_v2_canary is False


# --- completeness matrix -----------------------------------------------------


def test_cell_states_declared_exempt_missing() -> None:
    declared = _meta(
        probes=(ProbeFacet(name="p", kind="http", target="t"),),
    )
    exempt = _meta(
        exemptions=(Exemption(check_id="signals", reason="report-only service"),),
    )
    assert cell_state(declared, "probes") == "declared"
    assert cell_state(declared, "signals") == "MISSING"
    assert cell_state(exempt, "signals") == "exempt"
    assert cell_state(exempt, "backups") == "MISSING"


def test_matrix_flags_critical_probe_without_signal_facet() -> None:
    """The Infra-012.10 counterfactual (issue #541 AC): a Deployer declaring a
    critical ProbeFacet with no SignalFacet tier/debounce MUST be flagged."""
    attrs = {
        "platform/pager": _meta(
            "platform/pager",
            probes=(
                ProbeFacet(name="pager-http", kind="http", target="t", expected="200"),
            ),
        )
    }
    flags = consistency_flags(attrs)
    assert len(flags) == 1
    assert "platform/pager" in flags[0]
    assert "SignalFacet" in flags[0]


def test_matrix_accepts_critical_probe_with_signal_or_exemption() -> None:
    with_signal = {
        "platform/ok": _meta(
            "platform/ok",
            probes=(ProbeFacet(name="p", kind="http", target="t"),),
            signals=(
                SignalFacet(
                    tier="minute",
                    type="alert",
                    consecutive_failures=3,
                    renotify_window_sec=1800,
                ),
            ),
        )
    }
    assert consistency_flags(with_signal) == []
    with_exemption = {
        "platform/ok2": _meta(
            "platform/ok2",
            probes=(ProbeFacet(name="p", kind="http", target="t"),),
            exemptions=(
                Exemption(check_id="signals", reason="watchdog-signals.yaml owns this"),
            ),
        )
    }
    assert consistency_flags(with_exemption) == []
    warning_only = {
        "platform/warn": _meta(
            "platform/warn",
            probes=(ProbeFacet(name="p", kind="http", target="t", severity="warning"),),
        )
    }
    assert consistency_flags(warning_only) == []


def test_matrix_flags_stale_exemption_on_declared_facet() -> None:
    attrs = {
        "platform/stale": _meta(
            "platform/stale",
            probes=(ProbeFacet(name="p", kind="http", target="t", severity="warning"),),
            exemptions=(Exemption(check_id="probes", reason="outdated"),),
        )
    }
    flags = consistency_flags(attrs)
    assert len(flags) == 1
    assert "stale exemption" in flags[0]


def test_build_matrix_and_report_cover_every_service_and_column() -> None:
    attrs = {
        "platform/a": _meta(
            "platform/a",
            probes=(ProbeFacet(name="p", kind="http", target="t", severity="warning"),),
        ),
        "platform/b": _meta("platform/b"),
    }
    rows = build_matrix(attrs)
    assert [sid for sid, _ in rows] == ["platform/a", "platform/b"]
    assert rows[0][1] == {
        "probes": "declared",
        "signals": "MISSING",
        "backups": "MISSING",
    }
    report, clean = render_report(attrs)
    assert not clean
    assert "5 MISSING cell(s)" in report


def test_matrix_main_is_report_only_by_default(capsys) -> None:
    """The live repo currently has MISSING cells (the backlog) — the default
    invocation must print them and still exit 0 (ratchet phase 1)."""
    assert matrix_main([]) == 0
    out = capsys.readouterr().out
    assert "service × facet completeness matrix" in out
    for service_id in reg.all_services():
        assert service_id in out


def test_matrix_main_enforce_fails_on_missing_cells(capsys) -> None:
    # today's real registry still has MISSING cells, so --enforce must be red;
    # when the backlog is cleared this test should flip alongside the CI flag.
    report, clean = render_report(reg.service_attrs())
    assert matrix_main(["--enforce"]) == (0 if clean else 1)
