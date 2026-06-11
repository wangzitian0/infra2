# Infra2 Cloudflare Watchdog

Cloudflare Workers Cron out-of-band watchdog for infra2 public routes and probe
runner heartbeat freshness.

## Scope

- Runs every 30 minutes from Cloudflare, outside the infra2 VPS.
- Checks production public routes and the staging routes that are known to
  return intentional health statuses.
- Receives `/heartbeat` posts from `platform-alerting-probes${ENV_SUFFIX}`.
- Exposes authenticated `/status` for GitHub audit checks of Worker cron and KV
  state.
- Sends Feishu custom bot webhook messages directly, bypassing SigNoz and the
  internal alert bridge.
- Uses Workers KV to record heartbeat state, Worker last-run state, dedupe
  unchanged failures, renotify, and send one recovery message.
- Classifies malformed JSON or other config-parse errors as a separate
  config-preflight failure so configuration drift does not look like a route
  outage.
- Dedupe is keyed on stable identity plus failure domain, not volatile details
  like heartbeat age, so the same fault renotifies only on the configured
  interval.

## Required Secrets

For Feishu custom bot webhook mode:

```bash
wrangler secret put FEISHU_WEBHOOK_URL
```

For Feishu Open Platform app bot mode:

```bash
wrangler secret put FEISHU_APP_SECRET
```

`FEISHU_WEBHOOK_URL` must be a Feishu custom bot webhook when
`ALERT_DELIVERY_MODE=feishu_webhook`. `FEISHU_APP_SECRET` is required when
`ALERT_DELIVERY_MODE=feishu_app`.

Heartbeat:

```bash
wrangler secret put HEARTBEAT_TOKEN
wrangler secret put WATCHDOG_STATUS_TOKEN
```

`HEARTBEAT_TOKEN` must match `INFRA_PROBE_HEARTBEAT_TOKEN` in the platform
alerting deployment. `WATCHDOG_STATUS_TOKEN` is required for authenticated
GitHub audit checks of `/status`.

Secondary alert channel (email via Resend — used only when Feishu delivery
fails, so a Feishu outage cannot silently swallow an alert):

```bash
wrangler secret put RESEND_API_KEY
```

`ALERT_EMAIL_TO` / `ALERT_EMAIL_FROM` are set in `wrangler.toml`; the sender
domain must be verified in Resend. If `RESEND_API_KEY` (or `ALERT_EMAIL_TO`) is
unset, the email escalation is skipped — a `watchdog.delivery.escalation_unavailable`
event is logged and the original Feishu delivery failure is recorded as before
(the alert is not silently swallowed, and no spurious "all channels failed" is
raised).

## 1Password-backed Secret Sync

The Cloudflare Worker API token and watchdog status token are stored in
1Password item `Infra2/bootstrap/cloudflare-worker`:

- `CLOUDFLARE_WORKER_API_TOKEN`
- `WATCHDOG_STATUS_TOKEN`

If `WATCHDOG_STATUS_TOKEN` does not exist yet, create it in 1Password first:

```bash
env -u OP_SERVICE_ACCOUNT_TOKEN op item edit \
  'bootstrap/cloudflare-worker' \
  --vault=Infra2 \
  "WATCHDOG_STATUS_TOKEN[password]=$(openssl rand -base64 48)"
```

Then sync the 1Password value to Cloudflare and GitHub without printing it:

```bash
status_token="$(
  env -u OP_SERVICE_ACCOUNT_TOKEN op item get \
    'bootstrap/cloudflare-worker' \
    --vault=Infra2 \
    --fields label=WATCHDOG_STATUS_TOKEN \
    --reveal
)"
worker_api_token="$(
  env -u OP_SERVICE_ACCOUNT_TOKEN op item get \
    'bootstrap/cloudflare-worker' \
    --vault=Infra2 \
    --fields label=CLOUDFLARE_WORKER_API_TOKEN \
    --reveal
)"

printf '%s' "$status_token" | \
  CLOUDFLARE_API_TOKEN="$worker_api_token" wrangler secret put WATCHDOG_STATUS_TOKEN

printf '%s' "$status_token" | \
  gh secret set INFRA2_WATCHDOG_WORKER_STATUS_TOKEN --repo wangzitian0/infra2

unset status_token worker_api_token
```

Use `env -u OP_SERVICE_ACCOUNT_TOKEN` when the local shell has a stale deleted
1Password service-account token and the interactive 1Password session should be
used instead.

## Required KV

```bash
wrangler kv namespace create WATCHDOG_STATE
wrangler kv namespace create WATCHDOG_STATE --preview
```

Copy the namespace IDs into `wrangler.toml` before deployment.

## Deployment

```bash
cd cloudflare/infra-watchdog
wrangler deploy
```

> Merging `worker.js` to `main` does **not** deploy the worker. Run
> `wrangler deploy` after merge, otherwise the deployed worker drifts behind the
> repo (this is how an undeployed KV-throttle fix once let the worker exhaust the
> daily KV quota and emit false stale-heartbeat alerts).

### Observability (queryable logs)

`[observability]` is enabled in `wrangler.toml`, so `console.log` output is
persisted as Workers Logs (free-tier allocation) and is queryable in the
Cloudflare dashboard after the fact — not only via live `wrangler tail`.
Heartbeat storage failures emit a queryable `watchdog.heartbeat.error` event.

### Availability ledger & R2 cold archive

Every run records a single rolling daily rollup (`ledger:<date>` in KV) of
per-signal success/failure counts — positive proof that makes uptime%
queryable, not just failures. Finalized (past) days are cold-archived off-host
to R2 (`watchdog-ledger/<date>.json` in bucket `infra2`) for retention beyond
the KV hot window.

The archive is **reconciled idempotently on every run** (`reconcileArchives`),
not written once at the day rollover. The old one-shot lost a whole day's
archive silently if that single run hiccupped (an R2 blip, the `.date` migration
boundary, or a thrown `put`). The per-run reconciler `head()`-checks the last
`ARCHIVE_BACKFILL_DAYS` finalized days and writes only the ones missing, so it
retries until it sticks and backfills any gap. A write failure emits a queryable
`watchdog.ledger.archive` `status: "fail"` event instead of being swallowed, and
never crashes the run. Verify the archive positively with
`wrangler r2 object get infra2/watchdog-ledger/<yesterday>.json --pipe`.

### Free-quota safety

The worker must never trip the Cloudflare KV free-tier limit (1000 puts/day),
because a quota trip silently kills heartbeat tracking. Heartbeat writes are
throttled by `WATCHDOG_HEARTBEAT_MIN_WRITE_INTERVAL_SECONDS` (set to `900` in
`wrangler.toml`; the worker falls back to `600` if the variable is unset).
Worst-case daily puts (`heartbeat keys * ceil(86400/interval) + cron lastRun and
alert-state puts`) stay well under the limit, and
`tests/test_cloudflare_watchdog.py` asserts this budget. If
KV `put()` still fails, `recordHeartbeat` degrades to HTTP 200 with a logged
error instead of an unhandled 500/1101.

Set the probe runner heartbeat endpoint after deployment:

```bash
uv run invoke env.set INFRA_PROBE_HEARTBEAT_URL=https://infra2-cloudflare-watchdog.<account>.workers.dev/heartbeat --project=platform --env=production --service=alerting --credential-type=root_vars
uv run invoke env.set INFRA_PROBE_HEARTBEAT_TOKEN=<token> --project=platform --env=production --service=alerting --credential-type=root_vars
uv run invoke env.set INFRA_PROBE_HEARTBEAT_URL=https://infra2-cloudflare-watchdog.<account>.workers.dev/heartbeat --project=platform --env=staging --service=alerting --credential-type=root_vars
uv run invoke env.set INFRA_PROBE_HEARTBEAT_TOKEN=<token> --project=platform --env=staging --service=alerting --credential-type=root_vars
```

Then redeploy platform alerting for each environment.

## Optional Vars

- `WATCHDOG_ENVIRONMENTS`: comma-separated list, defaults to
  `production,staging`.
- `WATCHDOG_HTTP_TIMEOUT_MS`: defaults to `8000`.
- `WATCHDOG_RETRY_MAX_ATTEMPTS`: defaults to `2`.
- `WATCHDOG_RETRY_DELAY_MS`: defaults to `60000` (retry after one minute).
- `WATCHDOG_RENOTIFY_SECONDS`: defaults to `7200`.
- `WATCHDOG_STATUS_MAX_AGE_SECONDS`: defaults to `7200`.
- `WATCHDOG_TARGETS_JSON`: JSON array overriding public route targets.
- `WATCHDOG_HEARTBEATS_JSON`: JSON array overriding heartbeat checks.
- `ALERT_DELIVERY_MODE`: `feishu_webhook` or `feishu_app`.
- `FEISHU_APP_ID`: required for app bot mode.
- `FEISHU_CHAT_ID`: required for app bot mode.
- `FEISHU_API_BASE`: optional, defaults to `https://open.feishu.cn`.

The live deployment checks production and staging probe-runner heartbeats.
`cloud-staging` and `vault-staging` remain explicit exclusions while those
routes return HTTP 404; see `docs/ssot/watchdog-signals.yaml`.
