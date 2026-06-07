# Infra2 Cloudflare Watchdog

Cloudflare Workers Cron out-of-band watchdog for infra2 public routes and probe
runner heartbeat freshness.

## Scope

- Runs every 10 minutes from Cloudflare, outside the infra2 VPS.
- Checks production and staging public routes by default.
- Receives `/heartbeat` posts from `platform-alerting-probes${ENV_SUFFIX}`.
- Sends Feishu custom bot webhook messages directly, bypassing SigNoz and the
  internal alert bridge.
- Uses Workers KV to dedupe unchanged failures, renotify hourly, and send one
  recovery message.

## Required Secrets

```bash
wrangler secret put FEISHU_WEBHOOK_URL
wrangler secret put HEARTBEAT_TOKEN
```

`FEISHU_WEBHOOK_URL` must be a Feishu custom bot webhook. `HEARTBEAT_TOKEN` must
match `INFRA_PROBE_HEARTBEAT_TOKEN` in the platform alerting deployment.

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
- `WATCHDOG_RENOTIFY_SECONDS`: defaults to `3600`.
- `WATCHDOG_TARGETS_JSON`: JSON array overriding public route targets.
- `WATCHDOG_HEARTBEATS_JSON`: JSON array overriding heartbeat checks.
