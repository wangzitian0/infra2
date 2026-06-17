"""#368: the otel ingest endpoint + CORS origins have ONE source — and don't drift.

Before #368 the `otel` subdomain, the `/v1/traces` path, the full FE endpoint, and the
collector CORS allow-list were hardcoded/duplicated across two compose files,
``platform/11.signoz/deploy.py`` (a second literal separate from ``service_domain``),
and ``otel-collector-config.yaml`` (a hand-maintained list). These tests pin that:

  * the endpoint is built once (libs.common) and reused by deploy.py + both deploy paths,
  * the CORS allow-list is DERIVED from the FE origins (tools.deploy_env_config),
  * the rendered collector config + both compose files stay byte-for-behaviour identical
    to the pre-#368 effective values (endpoint + CORS origins unchanged).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

from libs.common import (
    OTEL_INGEST_SUBDOMAIN,
    OTLP_TRACES_PATH,
    otel_ingest_endpoint,
)
from tools.deploy_env_config import (
    cors_allowed_origins,
    otel_env,
    otel_ingest_endpoint as cfg_otel_ingest_endpoint,
)

ROOT = Path(__file__).resolve().parents[2]
DOMAIN = "zitian.party"

# The effective values BEFORE #368 (copied verbatim from the old literals). The whole
# point is that the de-hardcoded code must still produce exactly these.
_LEGACY_ENDPOINT = "https://otel.zitian.party/v1/traces"
_LEGACY_CORS_ORIGINS = [
    "https://report.zitian.party",
    "https://report-staging.zitian.party",
    "https://report-main.zitian.party",
    "https://report-pr-*.zitian.party",
    "https://report-commit-*.zitian.party",
    "http://localhost:3000",
]


def _load_signoz_deployer():
    path = ROOT / "platform/11.signoz/deploy.py"
    spec = importlib.util.spec_from_file_location("signoz_deploy_368", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SigNozDeployer


# --------------------------------------------------------------------------- #
# 1. Single source for the endpoint (subdomain + path constants, one builder)
# --------------------------------------------------------------------------- #
def test_endpoint_constants_are_the_one_source():
    assert OTEL_INGEST_SUBDOMAIN == "otel"
    assert OTLP_TRACES_PATH == "/v1/traces"


def test_endpoint_unchanged_for_production():
    env = {"INTERNAL_DOMAIN": DOMAIN, "ENV": "production"}
    assert otel_ingest_endpoint(env) == _LEGACY_ENDPOINT


def test_endpoint_empty_without_domain():
    assert otel_ingest_endpoint({"INTERNAL_DOMAIN": None}) == ""


def test_config_endpoint_helper_matches_common_source():
    # tools.deploy_env_config builds from the SAME libs.common constants.
    assert cfg_otel_ingest_endpoint(domain=DOMAIN) == _LEGACY_ENDPOINT


def test_otel_env_injects_single_sourced_endpoint():
    assert otel_env(domain=DOMAIN) == {
        "NEXT_PUBLIC_OTEL_EXPORTER_OTLP_ENDPOINT": _LEGACY_ENDPOINT
    }


# --------------------------------------------------------------------------- #
# 2. CORS origins derived from the FE convention, identical to the old list
# --------------------------------------------------------------------------- #
def test_cors_origins_unchanged():
    assert cors_allowed_origins(domain=DOMAIN) == _LEGACY_CORS_ORIGINS


def test_cors_origins_track_the_domain():
    # Derived (not literal): a different base domain re-renders every origin.
    origins = cors_allowed_origins(domain="example.test")
    assert "https://report.example.test" in origins
    assert "https://report-pr-*.example.test" in origins
    assert "http://localhost:3000" in origins  # local dev origin is domain-independent


# --------------------------------------------------------------------------- #
# 3. Rendered collector config — parses, and CORS == derived origins
# --------------------------------------------------------------------------- #
def test_rendered_collector_config_parses_and_uses_derived_cors():
    D = _load_signoz_deployer()
    env = {"INTERNAL_DOMAIN": DOMAIN, "ENV_SUFFIX": "", "ENV": "production"}
    rendered = D._render_collector_config(env)
    # No unsubstituted placeholders survive.
    assert "${OTEL_CORS_ALLOWED_ORIGINS}" not in rendered
    assert "${ENV_SUFFIX}" not in rendered
    doc = yaml.safe_load(rendered)
    cors = doc["receivers"]["otlp"]["protocols"]["http"]["cors"]
    assert cors["allowed_origins"] == _LEGACY_CORS_ORIGINS


def test_rendered_collector_config_applies_env_suffix():
    D = _load_signoz_deployer()
    env = {"INTERNAL_DOMAIN": DOMAIN, "ENV_SUFFIX": "-staging", "ENV": "production"}
    doc = yaml.safe_load(D._render_collector_config(env))
    dsn = doc["exporters"]["clickhousetraces"]["datasource"]
    assert "platform-clickhouse-staging:9000" in dsn


# --------------------------------------------------------------------------- #
# 4. Both compose files consume the injected endpoint (no inline re-construction)
# --------------------------------------------------------------------------- #
COMPOSE_FILES = [
    ROOT / "finance_report/finance_report/10.app/compose.yaml",
    ROOT / "finance_report/finance_report/preview/compose.yaml",
]


def test_compose_files_parse_and_consume_injected_endpoint():
    for path in COMPOSE_FILES:
        doc = yaml.safe_load(path.read_text())
        assert doc, f"{path} did not parse"
        env = doc["services"]["frontend"]["environment"]
        value = env["NEXT_PUBLIC_OTEL_EXPORTER_OTLP_ENDPOINT"]
        # Consumes the injected var (single source), with the identical fallback default.
        assert value.startswith("${NEXT_PUBLIC_OTEL_EXPORTER_OTLP_ENDPOINT:-"), (
            f"{path} re-constructs the endpoint instead of consuming the injected one"
        )
        # The fallback default must equal the legacy effective value once interpolated.
        assert "https://otel.${INTERNAL_DOMAIN}/v1/traces}" in value
