import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SERVICE_DIR = ROOT / "truealpha/truealpha/10.app"


def _load_deploy_module():
    spec = importlib.util.spec_from_file_location("truealpha_app_deploy", SERVICE_DIR / "deploy.py")
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
    rule_label = next(label for label in llm_labels if ".rule=" in label and "truealpha-api" in label)
    rule = rule_label.split(".rule=", 1)[1]

    assert "PathPrefix(`/api`)" not in rule, "the llm router must not claim the bare /api prefix"
    assert "PathPrefix(`/api/mcp`)" in rule
    assert "Path(`/api/health`)" in rule

    web_labels = compose["services"]["web"]["labels"]
    web_rule_label = next(label for label in web_labels if ".rule=" in label and "truealpha-web" in label)
    # web's router must stay a bare Host() match (no PathPrefix of its own) so it
    # remains the catch-all for every /api/* path the llm router no longer claims.
    assert "PathPrefix" not in web_rule_label.split(".rule=", 1)[1]


def test_both_routers_use_the_computed_app_host_not_the_literal_prefix_pattern() -> None:
    """truealpha#474: `truealpha${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}` collapses to
    the malformed `truealpha.truealpha.club` in production (empty ENV_DOMAIN_SUFFIX,
    INTERNAL_DOMAIN already `truealpha.club`). Both routers must use the
    deploy.py-computed ${APP_HOST} instead."""
    compose = yaml.safe_load((SERVICE_DIR / "compose.yaml").read_text(encoding="utf-8"))
    for service in ("llm", "web"):
        labels = compose["services"][service]["labels"]
        router_name = "truealpha-api" if service == "llm" else "truealpha-web"
        rule = next(label for label in labels if ".rule=" in label and router_name in label).split(".rule=", 1)[1]
        assert "${APP_HOST}" in rule
        assert "${INTERNAL_DOMAIN}" not in rule


def test_app_host_is_bare_domain_in_production_and_prefixed_elsewhere() -> None:
    """truealpha#474: production reaches the bare domain (no redundant "truealpha"
    prefix); non-production keeps the "truealpha" prefix, preserving staging's
    existing, already-working truealpha-staging.<domain> shape unchanged.

    compose_env_overrides, not compose_env_base: libs.deploy.promote.deploy (the
    actual staging/prod deploy path for this fixed app) never calls
    compose_env_base at all -- see test_deploy_primitive.py's
    test_truealpha_*_deploy_sets_app_host for the regression test against that
    real path (this file only pins the formula in isolation)."""
    module = _load_deploy_module()
    deployer = module.AppDeployer

    assert deployer.compose_env_overrides(env="production", domain="truealpha.club", env_suffix="")[
        "APP_HOST"
    ] == "truealpha.club"
    assert deployer.compose_env_overrides(env="staging", domain="zitian.party", env_suffix="-staging")[
        "APP_HOST"
    ] == "truealpha-staging.zitian.party"
