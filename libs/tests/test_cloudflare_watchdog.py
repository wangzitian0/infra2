"""Offline contract tests for the Cloudflare out-of-band watchdog."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKER_DIR = ROOT / "cloudflare/infra-watchdog"
WORKER = WORKER_DIR / "worker.js"
WRANGLER = WORKER_DIR / "wrangler.toml"
README = WORKER_DIR / "README.md"


def test_cloudflare_watchdog_runs_every_ten_minutes_with_kv_state() -> None:
    """Infra-011.2: Cloudflare watchdog is stateful and lower cost than Actions."""
    config = WRANGLER.read_text(encoding="utf-8")

    assert 'crons = ["*/10 * * * *"]' in config
    assert 'binding = "WATCHDOG_STATE"' in config
    assert 'WATCHDOG_ENVIRONMENTS = "production,staging"' in config
    assert "FEISHU_WEBHOOK_URL" not in config
    assert "HEARTBEAT_TOKEN" not in config


def test_worker_default_targets_cover_production_and_staging_routes() -> None:
    """Infra-011.2: prod and staging public routes are explicit defaults."""
    source = WORKER.read_text(encoding="utf-8")

    for host in [
        "https://cloud.zitian.party",
        "https://vault.zitian.party/v1/sys/health",
        "https://minio.zitian.party/minio/health/live",
        "https://sso.zitian.party/-/health/live/",
        "https://signoz.zitian.party",
        "https://cloud-staging.zitian.party",
        "https://vault-staging.zitian.party/v1/sys/health",
        "https://minio-staging.zitian.party/minio/health/live",
        "https://sso-staging.zitian.party/-/health/live/",
        "https://signoz-staging.zitian.party",
    ]:
        assert host in source

    assert '"production"' in source
    assert '"staging"' in source
    assert "[200, 429, 472, 473]" in source


def test_worker_heartbeat_endpoint_and_staleness_checks_are_required() -> None:
    """Infra-011.2: the external watchdog detects the probe runner going stale."""
    source = WORKER.read_text(encoding="utf-8")

    assert 'url.pathname === "/heartbeat"' in source
    assert "HEARTBEAT_TOKEN" in source
    assert "heartbeatKey(environment, name)" in source
    assert "`heartbeat:${environment}:${name}`" in source
    assert "platform-alerting-probes" in source
    assert "platform-alerting-probes-staging" in source
    assert "heartbeat stale" in source
    assert "heartbeat missing" in source
    assert "heartbeat reports unhealthy" in source


def test_worker_dedupes_renotifies_and_sends_recovery_to_feishu() -> None:
    """Infra-011.2: Cloudflare alerts must not repeat every cron tick."""
    source = WORKER.read_text(encoding="utf-8")

    assert "alert-state:cloudflare-watchdog" in source
    assert "WATCHDOG_RENOTIFY_SECONDS" in source
    assert "failureFingerprint" in source
    assert "formatResolvedMessage" in source
    assert "Infra2 Cloudflare watchdog failed" in source
    assert "Infra2 Cloudflare watchdog recovered" in source
    assert "Cloudflare Workers Cron -> Feishu direct" in source


def test_worker_supports_existing_feishu_app_bot_mode() -> None:
    """Infra-011.2: Cloudflare watchdog can reuse platform alerting app bot secrets."""
    source = WORKER.read_text(encoding="utf-8")

    assert "ALERT_DELIVERY_MODE" in source
    assert "feishu_app" in source
    assert "FEISHU_APP_ID" in source
    assert "FEISHU_APP_SECRET" in source
    assert "FEISHU_CHAT_ID" in source
    assert "/open-apis/auth/v3/tenant_access_token/internal" in source
    assert "/open-apis/im/v1/messages?receive_id_type=chat_id" in source


def test_cloudflare_watchdog_docs_include_deploy_and_secret_contract() -> None:
    """Infra-011.2: setup is documented without committing credentials."""
    readme = README.read_text(encoding="utf-8")

    assert "wrangler secret put FEISHU_WEBHOOK_URL" in readme
    assert "wrangler secret put FEISHU_APP_SECRET" in readme
    assert "wrangler secret put HEARTBEAT_TOKEN" in readme
    assert "wrangler kv namespace create WATCHDOG_STATE" in readme
    assert "INFRA_PROBE_HEARTBEAT_URL" in readme
    assert "INFRA_PROBE_HEARTBEAT_TOKEN" in readme
    assert "production,staging" in readme
