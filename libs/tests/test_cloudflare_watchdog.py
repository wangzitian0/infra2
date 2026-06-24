"""Offline contract tests for the Cloudflare out-of-band watchdog."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKER_DIR = ROOT / "cloudflare/infra-watchdog"
WORKER = WORKER_DIR / "worker.js"
WRANGLER = WORKER_DIR / "wrangler.toml"
README = WORKER_DIR / "README.md"


def test_cloudflare_watchdog_runs_every_thirty_minutes_with_kv_state() -> None:
    """Infra-011.2: Cloudflare watchdog is stateful and lower cost than Actions."""
    config = WRANGLER.read_text(encoding="utf-8")

    assert 'crons = ["*/30 * * * *"]' in config
    assert 'binding = "WATCHDOG_STATE"' in config
    assert 'WATCHDOG_ENVIRONMENTS = "production,staging"' in config
    assert 'WATCHDOG_RENOTIFY_SECONDS = "7200"' in config
    assert 'WATCHDOG_STATUS_MAX_AGE_SECONDS = "7200"' in config
    assert 'WATCHDOG_RETRY_MAX_ATTEMPTS = "2"' in config
    assert 'WATCHDOG_RETRY_DELAY_MS = "60000"' in config
    assert "FEISHU_WEBHOOK_URL" not in config
    assert "HEARTBEAT_TOKEN" not in config


def test_worker_default_targets_cover_enabled_public_routes_only() -> None:
    """Infra-011.2: unavailable staging routes must be explicit exclusions."""
    source = WORKER.read_text(encoding="utf-8")

    for host in [
        "https://cloud.zitian.party",
        "https://vault.zitian.party/v1/sys/health",
        "https://minio.zitian.party/minio/health/live",
        "https://sso.zitian.party/-/health/live/",
        "https://signoz.zitian.party",
        "https://minio-staging.zitian.party/minio/health/live",
        "https://sso-staging.zitian.party/-/health/live/",
        "https://report.zitian.party/",
        "https://report.zitian.party/api/health",
        "https://report-staging.zitian.party/",
        "https://report-staging.zitian.party/api/health",
    ]:
        assert host in source

    assert '"production"' in source
    assert '"staging"' in source
    assert "[200, 429, 472, 473]" in source
    assert "https://cloud-staging.zitian.party" not in source
    assert "https://vault-staging.zitian.party/v1/sys/health" not in source
    # signoz is a single global instance (prod_only); there is no staging
    # deployment by design, so signoz-staging.zitian.party must not be probed.
    assert "https://signoz-staging.zitian.party" not in source


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
    assert "heartbeat timestamp is in the future" in source


def test_worker_exposes_authenticated_status_for_github_audit() -> None:
    """#209: GitHub audit must detect Worker cron/KV/effective-config blindness."""
    source = WORKER.read_text(encoding="utf-8")

    assert 'url.pathname === "/status"' in source
    assert "WATCHDOG_STATUS_TOKEN" in source
    assert '"watchdog:last-run"' in source
    assert "routeTargetCount" in source
    assert "heartbeatTargetCount" in source
    assert "deliveryError" in source
    assert "watchdog delivery failed" in source
    assert "effective public route target list is empty" in source
    assert "effective heartbeat target list is empty" in source
    assert "cloudflare-watchdog-config-preflight" in source
    assert "config-preflight failed" in source


def test_worker_dedupes_renotifies_and_sends_recovery_to_feishu() -> None:
    """Infra-011.2: Cloudflare alerts must not repeat every cron tick."""
    source = WORKER.read_text(encoding="utf-8")

    assert "alert-state:cloudflare-watchdog" in source
    assert "WATCHDOG_RENOTIFY_SECONDS" in source
    assert "failureFingerprint" in source
    assert "checkHttpTargetWithRetry" in source
    assert "WATCHDOG_RETRY_MAX_ATTEMPTS" in source
    assert "WATCHDOG_RETRY_DELAY_MS" in source
    assert 'event: "watchdog.check"' in source
    assert 'event: "watchdog.run"' in source
    assert 'event: "watchdog.delivery.failure"' in source
    assert 'failureDomain: failure.failure_domain || ""' in source
    assert "failure_domain: failureDomain" in source
    assert "_failure_domain_for_http_target" in source
    assert "_failure_domain_for_heartbeat" in source
    assert 'return "public-route";' in source
    assert 'return "heartbeat";' in source
    assert "detail: failure.detail" not in source
    assert "formatResolvedMessage" in source
    assert "suggestedActionForFailure" in source
    assert "runbookUrlForDomain" in source
    assert 'url: target.url || ""' in source
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


def test_worker_escalates_to_email_when_feishu_delivery_fails() -> None:
    """A Feishu outage must not silently swallow an alert: email is the
    independent secondary channel, sent only when Feishu delivery fails."""
    source = WORKER.read_text(encoding="utf-8")
    config = WRANGLER.read_text(encoding="utf-8")

    # Alerts go through the dual-channel wrapper, not raw sendFeishu.
    assert "async function deliverAlert(env, text, kind)" in source
    assert "deliverAlert(env, formatFailureMessage(failures)" in source
    assert "deliverAlert(env, formatResolvedMessage()" in source
    # Email is sent inside the Feishu-failure catch (escalation, not duplicate).
    assert "async function sendEmail(env, subject, text)" in source
    assert "https://api.resend.com/emails" in source
    assert 'event: "watchdog.delivery.escalated"' in source
    assert "all alert channels failed" in source
    # Config: recipient + sender in wrangler, RESEND_API_KEY is a secret.
    assert "ALERT_EMAIL_TO" in config
    assert "wangzitian.ai@gmail.com" in config
    assert "RESEND_API_KEY" in source


def test_cloudflare_watchdog_docs_include_deploy_and_secret_contract() -> None:
    """Infra-011.2: setup is documented without committing credentials."""
    readme = README.read_text(encoding="utf-8")
    ssot = (ROOT / "docs/ssot/ops.observability.md").read_text(encoding="utf-8")

    assert "wrangler secret put FEISHU_WEBHOOK_URL" in readme
    assert "wrangler secret put FEISHU_APP_SECRET" in readme
    assert "wrangler secret put HEARTBEAT_TOKEN" in readme
    assert "wrangler secret put WATCHDOG_STATUS_TOKEN" in readme
    assert "wrangler kv namespace create WATCHDOG_STATE" in readme
    assert "INFRA_PROBE_HEARTBEAT_URL" in readme
    assert "INFRA_PROBE_HEARTBEAT_TOKEN" in readme
    assert "production,staging" in readme
    assert "stable identity plus failure domain" in readme
    assert "stable failure identity plus failure domain" in ssot


def test_worker_heartbeat_throttles_kv_writes_to_avoid_daily_limit() -> None:
    """Heartbeat writes must be throttled so the KV daily put() limit isn't hit."""
    source = WORKER.read_text(encoding="utf-8")

    # Read-then-maybe-write: a status change persists immediately, otherwise the
    # write is throttled by a configurable minimum interval.
    assert "WATCHDOG_HEARTBEAT_MIN_WRITE_INTERVAL_SECONDS" in source
    assert "shouldWrite" in source
    assert "statusUnchanged" in source
    assert "const existingRaw = await env.WATCHDOG_STATE.get(key)" in source


def test_worker_records_availability_ledger_with_positive_proof() -> None:
    """Infra-012.4: every run records per-signal success+failure for uptime%."""
    source = WORKER.read_text(encoding="utf-8")

    # One aggregated rollup write per run (positive proof = success counts), not
    # one key per signal, so the KV free tier stays safe.
    assert "async function recordLedger(env, results, nowMs)" in source
    assert "await recordLedger(env, allResults, nowMs)" in source
    assert "entry.ok += 1" in source
    assert "entry.fail += 1" in source
    # Single rolling daily key + bounded retention.
    assert "function ledgerKey(date)" in source
    assert "const LEDGER_RETENTION_DAYS = 21" in source
    assert "WATCHDOG_STATE.delete" in source


def test_worker_archives_finalized_day_to_r2_off_host() -> None:
    """ops.availability_ledger: cold archive to R2 (single off-host store, S3)."""
    source = WORKER.read_text(encoding="utf-8")
    config = WRANGLER.read_text(encoding="utf-8")

    # Guarded R2 write so a missing binding is a no-op, not a deploy/runtime crash.
    assert "if (!env.LEDGER_BUCKET)" in source
    assert "env.LEDGER_BUCKET.put(" in source
    assert "watchdog-ledger/" in source
    # R2 binding declared in wrangler, reusing the existing off-host backup bucket.
    assert 'binding = "LEDGER_BUCKET"' in config
    assert 'bucket_name = "infra2"' in config


def test_worker_self_heals_missing_r2_archives_idempotently() -> None:
    """ops.availability_ledger: cold archive is reconciled every run (self-heal).

    A one-shot "archive yesterday on the first run of a new day" silently loses a
    whole day if that single run hiccups (an R2 blip, the .date migration
    boundary, a thrown put). The archive must instead be reconciled idempotently
    on every run -- retried until it sticks, backfilling any gap -- and a write
    failure must surface as a queryable structured event, never be swallowed.
    """
    source = WORKER.read_text(encoding="utf-8")

    # Per-run reconciliation independent of isNewDay.
    assert "async function reconcileArchives(env, nowMs)" in source
    assert "await reconcileArchives(env, nowMs)" in source
    # Existence check before write => idempotent backfill, puts only when missing.
    assert "env.LEDGER_BUCKET.head(" in source
    # Write failures are observable via the same structured-log channel, not silent.
    assert 'event: "watchdog.ledger.archive"' in source
    # Reconciliation must NOT be gated solely on the day rollover anymore.
    assert "if (isNewDay) {\n    if (env.LEDGER_BUCKET) {" not in source


def test_worker_exposes_token_protected_ledger_endpoint() -> None:
    """Infra-012: GitHub jobs read the ledger over a bearer-protected route."""
    source = WORKER.read_text(encoding="utf-8")

    assert 'url.pathname === "/ledger"' in source
    assert "async function ledgerResponse(request, env)" in source
    # Reuses the same bearer token as /status; never public.
    assert "WATCHDOG_STATUS_TOKEN" in source
    assert "window_days" in source


def test_worker_logs_are_queryable_via_observability() -> None:
    """Requirement: watchdog logs must be queryable after the fact, not tail-only."""
    config = WRANGLER.read_text(encoding="utf-8")

    assert "[observability]" in config
    assert "enabled = true" in config


def test_worker_heartbeat_interval_is_explicit_and_within_kv_budget() -> None:
    """Requirement: the watchdog must never trip the Cloudflare KV free quota."""
    import json
    import math
    import tomllib

    config = tomllib.loads(WRANGLER.read_text(encoding="utf-8"))
    variables = config["vars"]

    interval_seconds = int(variables["WATCHDOG_HEARTBEAT_MIN_WRITE_INTERVAL_SECONDS"])
    assert interval_seconds >= 600

    heartbeats = json.loads(variables["WATCHDOG_HEARTBEATS_JSON"])
    cron_runs_per_day = 48  # every 30 minutes
    # Worst case: every heartbeat key forces one write per throttle interval, plus
    # one lastRun put and one alert-state put per cron run. Ceil the per-key
    # writes so a non-divisible interval is not under-counted.
    heartbeat_puts = len(heartbeats) * math.ceil(86400 / interval_seconds)
    cron_puts = cron_runs_per_day * 2
    worst_case_daily_puts = heartbeat_puts + cron_puts

    kv_free_daily_put_limit = 1000
    assert worst_case_daily_puts < kv_free_daily_put_limit * 0.5, (
        f"worst-case KV puts/day={worst_case_daily_puts} too close to "
        f"free-tier limit {kv_free_daily_put_limit}"
    )


def test_worker_heartbeat_handler_is_failsafe_on_kv_errors() -> None:
    """A watcher's own storage failure must degrade visibly, not throw 500/1101."""
    source = WORKER.read_text(encoding="utf-8")

    assert 'event: "watchdog.heartbeat.error"' in source
    assert "degraded: true" in source
    # request body parsing must not throw on malformed input either.
    assert "payload = await request.json();" in source
    assert "payload = {};" in source
