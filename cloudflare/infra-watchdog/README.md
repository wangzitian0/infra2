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
```

`HEARTBEAT_TOKEN` must match `INFRA_PROBE_HEARTBEAT_TOKEN` in the platform
alerting deployment.

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
- `ALERT_DELIVERY_MODE`: `feishu_webhook` or `feishu_app`.
- `FEISHU_APP_ID`: required for app bot mode.
- `FEISHU_CHAT_ID`: required for app bot mode.
- `FEISHU_API_BASE`: optional, defaults to `https://open.feishu.cn`.

The live deployment currently checks production heartbeat only. Staging public
routes are checked, but staging heartbeat should be enabled only after the
staging alerting compose is confirmed to recreate its probe runner reliably.
