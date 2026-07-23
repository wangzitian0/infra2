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

    assert deployer.compose_env_overrides(env="production", domain="truealpha.club", env_suffix="")[
        "APP_HOST"
    ] == "truealpha.club"
    assert deployer.compose_env_overrides(env="staging", domain="zitian.party", env_suffix="-staging")[
        "APP_HOST"
    ] == "truealpha-staging.zitian.party"

    prod_env = {"ENV": "production", "ENV_DOMAIN_SUFFIX": "", "INTERNAL_DOMAIN": "truealpha.club"}
    assert deployer.compose_env_base(prod_env)["APP_HOST"] == "truealpha.club"

    # ENV_SUFFIX (data-path collision guard, distinct from ENV_DOMAIN_SUFFIX) must be
    # set for any non-production env or the base compose_env_base() raises ValueError.
    staging_env = {
        "ENV": "staging",
        "ENV_SUFFIX": "-staging",
        "ENV_DOMAIN_SUFFIX": "-staging",
        "INTERNAL_DOMAIN": "zitian.party",
    }
    assert deployer.compose_env_base(staging_env)["APP_HOST"] == "truealpha-staging.zitian.party"
