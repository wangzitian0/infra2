import importlib.util
from pathlib import Path

import pytest
import yaml

from libs.tests.compose_env import compose_services, raw_environment

ROOT = Path(__file__).resolve().parents[2]
SERVICE_DIR = ROOT / "truealpha/truealpha/10.app"
TRUEALPHA_COMPOSES = sorted((ROOT / "truealpha/truealpha").glob("*/compose.yaml"))
COLLECTOR_OTLP_HTTP = "http://platform-signoz-otel-collector:4318"


def _load_deploy_module():
    spec = importlib.util.spec_from_file_location(
        "truealpha_app_deploy", SERVICE_DIR / "deploy.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_shared_tasks_module():
    spec = importlib.util.spec_from_file_location(
        "truealpha_app_shared_tasks", SERVICE_DIR / "shared_tasks.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_api_router_does_not_claim_the_bare_api_prefix() -> None:
    """truealpha#463: a bare PathPrefix(`/api`) on the llm router claims every
    /api/* path on the shared Host, including web's own Next.js API routes
    (/api/auth/login, /api/auth/me) -- silently 404ing them at llm instead of
    ever reaching web. Login was broken on both staging and production for
    this exact reason. The llm router's rule must stay scoped to exactly what
    apps/llm-service/src/llm_service/main.py serves (GET /health, and the MCP
    app mounted at /mcp) so the rest of /api/* falls through to web's
    lower-priority Host()-only router."""
    compose = yaml.safe_load((SERVICE_DIR / "compose.yaml").read_text(encoding="utf-8"))
    llm_labels = compose["services"]["llm"]["labels"]
    rule_label = next(
        label for label in llm_labels if ".rule=" in label and "truealpha-api" in label
    )
    rule = rule_label.split(".rule=", 1)[1]

    assert "PathPrefix(`/api`)" not in rule, (
        "the llm router must not claim the bare /api prefix"
    )
    assert "PathPrefix(`/api/mcp`)" in rule
    assert "Path(`/api/health`)" in rule

    web_labels = compose["services"]["web"]["labels"]
    web_rule_label = next(
        label for label in web_labels if ".rule=" in label and "truealpha-web" in label
    )
    # web's router must stay a bare Host() match (no PathPrefix of its own) so it
    # remains the catch-all for every /api/* path the llm router no longer claims.
    assert "PathPrefix" not in web_rule_label.split(".rule=", 1)[1]


def test_both_routers_use_the_computed_app_host_not_the_literal_prefix_pattern() -> (
    None
):
    """truealpha#474: `truealpha${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}` collapses to
    the malformed `truealpha.truealpha.club` in production (empty ENV_DOMAIN_SUFFIX,
    INTERNAL_DOMAIN already `truealpha.club`). Both routers must use the
    deploy.py-computed ${APP_HOST} instead."""
    compose = yaml.safe_load((SERVICE_DIR / "compose.yaml").read_text(encoding="utf-8"))
    for service in ("llm", "web"):
        labels = compose["services"][service]["labels"]
        router_name = "truealpha-api" if service == "llm" else "truealpha-web"
        rule = next(
            label for label in labels if ".rule=" in label and router_name in label
        ).split(".rule=", 1)[1]
        assert "${APP_HOST}" in rule
        assert "${INTERNAL_DOMAIN}" not in rule


def test_app_host_is_bare_domain_in_production_and_prefixed_elsewhere() -> None:
    """truealpha#474: production reaches the bare domain (no redundant "truealpha"
    prefix); non-production keeps the "truealpha" prefix, preserving staging's
    existing, already-working truealpha-staging.<domain> shape unchanged.

    This app has TWO real deploy paths and both must independently end up with
    APP_HOST (see test_deploy_primitive.py's test_truealpha_*_deploy_sets_app_host
    for promote.deploy's path, the one this file can't reach without mocking
    Dokploy): libs.deploy.promote.deploy calls compose_env_overrides directly;
    Deployer.composing() (invoke ta-app.sync, auto-triggered by the iac-runner's
    live GitHub push webhook on every push touching this directory) calls
    compose_env_base instead and REPLACES the compose's whole env wholesale --
    omitting APP_HOST there silently wiped it on the very next auto-sync during
    development of this fix, independent of and after the promote.deploy fix
    landed. Pin both."""
    module = _load_deploy_module()
    deployer = module.AppDeployer

    assert (
        deployer.compose_env_overrides(
            env="production", domain="truealpha.club", env_suffix=""
        )["APP_HOST"]
        == "truealpha.club"
    )
    assert (
        deployer.compose_env_overrides(
            env="staging", domain="zitian.party", env_suffix="-staging"
        )["APP_HOST"]
        == "truealpha-staging.zitian.party"
    )

    prod_env = {
        "ENV": "production",
        "ENV_DOMAIN_SUFFIX": "",
        "INTERNAL_DOMAIN": "truealpha.club",
    }
    assert deployer.compose_env_base(prod_env)["APP_HOST"] == "truealpha.club"

    # ENV_SUFFIX (data-path collision guard, distinct from ENV_DOMAIN_SUFFIX) must be
    # set for any non-production env or the base compose_env_base() raises ValueError.
    staging_env = {
        "ENV": "staging",
        "ENV_SUFFIX": "-staging",
        "ENV_DOMAIN_SUFFIX": "-staging",
        "INTERNAL_DOMAIN": "zitian.party",
    }
    assert (
        deployer.compose_env_base(staging_env)["APP_HOST"]
        == "truealpha-staging.zitian.party"
    )


def test_status_combines_boolean_readiness_instead_of_truthy_result_dicts(monkeypatch):
    module = _load_shared_tasks_module()
    results = iter(
        [
            {"is_ready": False, "details": "Unhealthy"},
            {"is_ready": True, "details": "Healthy"},
        ]
    )
    monkeypatch.setattr(
        module, "check_service", lambda *_args, **_kwargs: next(results)
    )

    status = module.status.body(object())

    assert status == {
        "is_ready": False,
        "details": "llm=Unhealthy, web=Healthy",
    }


def test_preview_api_router_does_not_claim_the_bare_api_prefix() -> None:
    """Issue #803: Preview compose llm router must match production/staging routing
    and not claim the bare /api prefix, which would swallow web API routes."""
    preview_compose = yaml.safe_load(
        (ROOT / "truealpha/truealpha/preview/compose.yaml").read_text(encoding="utf-8")
    )
    llm_labels = preview_compose["services"]["llm"]["labels"]
    rule_label = next(
        label for label in llm_labels if ".rule=" in label and "truealpha-api" in label
    )
    rule = rule_label.split(".rule=", 1)[1]

    assert "PathPrefix(`/api`)" not in rule, (
        "the preview llm router must not claim the bare /api prefix"
    )
    assert "PathPrefix(`/api/mcp`)" in rule
    assert "Path(`/api/health`)" in rule


def _services_exporting_otlp(compose: Path) -> set[str]:
    return {
        name
        for name, service in compose_services(compose).items()
        if "OTEL_EXPORTER_OTLP_ENDPOINT" in raw_environment(service)
    }


def test_the_guard_covers_every_truealpha_compose() -> None:
    """The telemetry guards below govern what truealpha's composes may do, not today's
    three files: a compose added later is in scope by the glob, and an empty glob (a
    moved directory) fails here instead of turning every guard green-while-empty."""
    names = {compose.parent.name for compose in TRUEALPHA_COMPOSES}
    assert {"10.app", "20.data_engine", "preview"} <= names


@pytest.mark.parametrize("compose", TRUEALPHA_COMPOSES, ids=lambda c: c.parent.name)
def test_otlp_export_is_only_ever_enabled_with_the_issued_identity(
    compose: Path,
) -> None:
    """The endpoint turns export on, and truealpha's services refuse to start when it is
    on without the deployment-issued identity (infra2#906 / truealpha#1034). So a
    container naming the endpoint must take BOTH identity variables verbatim from the
    deploy-issued env -- not hardcoded and not defaulted -- and must be able to reach the
    collector, which is Docker-network-only (never published, never on the host network).
    """
    for name, service in compose_services(compose).items():
        env = raw_environment(service)
        names_collector = any(
            "platform-signoz-otel-collector" in v for v in env.values()
        )
        if service.get("network_mode") == "host":
            assert not names_collector, (
                f"{compose.parent.name}/{name} is on the host network, where the "
                "collector's Docker DNS name does not resolve"
            )
        if "OTEL_EXPORTER_OTLP_ENDPOINT" not in env:
            continue
        where = f"{compose.parent.name}/{name}"
        assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == COLLECTOR_OTLP_HTTP, where
        assert service.get("network_mode") != "host", where
        assert env.get("OTEL_SERVICE_NAME") == "${OTEL_SERVICE_NAME:-}", where
        assert env.get("OTEL_RESOURCE_ATTRIBUTES") == "${OTEL_RESOURCE_ATTRIBUTES:-}", (
            where
        )


@pytest.mark.parametrize("stack", ["10.app", "preview"])
def test_only_the_llm_service_exports_telemetry(stack: str) -> None:
    """llm (FastAPI, infra2-sdk configure_telemetry) is the one Python HTTP service. web is
    Node with no OTel SDK, and the single compose-level OTEL_SERVICE_NAME the deployment
    issues cannot name two services, so handing web the endpoint would mislabel it."""
    compose = ROOT / "truealpha/truealpha" / stack / "compose.yaml"
    assert _services_exporting_otlp(compose) == {"llm"}


def test_data_engine_stays_off_the_collector_while_it_runs_on_the_host_network() -> (
    None
):
    """The collector is Docker-network-only (ops.observability.md 4.2) and all three Dagster
    roles run with network_mode: host for OpenD, so the Docker DNS name cannot resolve
    from them and publishing the collector port is not this stack's call. Until a
    host-reachable ingest exists, the data engine exports nothing -- and is handed no
    identity either, so the SDK's endpoint-gated fail-fast never engages."""
    compose = ROOT / "truealpha/truealpha/20.data_engine/compose.yaml"
    services = compose_services(compose)
    assert {n for n, s in services.items() if s.get("network_mode") == "host"} == {
        "dagster-webserver",
        "dagster-daemon",
        "dagster-code-server",
    }
    assert _services_exporting_otlp(compose) == set()
    for service in services.values():
        assert not any(key.startswith("OTEL_") for key in raw_environment(service))


def test_both_issuing_paths_name_the_app_service_alike() -> None:
    """`ta-app.sync` (this Deployer) and promote / preview (ServiceSpec) each issue
    OTEL_SERVICE_NAME into the same compose. If they disagree the compose changes identity
    with whichever path deployed it last, splitting one service across two SigNoz names."""
    from libs.deploy.contract import service_spec
    from libs.service_registry import service_attrs

    deployer = _load_deploy_module().AppDeployer
    spec = service_spec("truealpha/app")

    assert deployer.telemetry_service_name == "truealpha-app"
    assert deployer.telemetry_service_name == spec.resolved_identity_service_name()
    assert (deployer.telemetry_component or deployer.service) == spec.identity_component
    assert service_attrs()["truealpha/app"].telemetry_service_name == "truealpha-app"
