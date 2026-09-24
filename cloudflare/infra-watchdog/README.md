# Infra2 Cloudflare Watchdog

Cloudflare Workers Cron out-of-band watchdog. It judges only what the infra2 VPS
cannot report about itself ([ops.observability.md §1.1](../../docs/ssot/ops.observability.md)):
everything else — the route inventory, retries, availability counting, archives,
reports — runs on the VPS (#904).

## Scope

Every 30 minutes, from Cloudflare, outside the VPS:

| Check | Pages | Class |
|---|---|---|
| Production probe-runner heartbeat older than `maxAgeSeconds` (or missing) | P0 "VPS 或它的出网中断" (the VPS or its egress is down) | host-reachability |
| Production v2 heartbeat fresh with `ok=false` (the probe loop or its delivery is unhealthy) for 2 consecutive runs; resolves after 2 healthy runs | P1, naming the runner's `detail` | alert-pipeline |
| One external entrypoint per product (`dokploy`, `finance-report-web`, `truealpha-web`, production) fails 2 consecutive runs | P0 | host-reachability |
| Staging heartbeat | never; shown on `/status` | — |

- **One GET per entrypoint per run.** No in-run retry or sleep; the debounce is
  `WATCHDOG_ENTRYPOINT_FAILURE_RUNS` (2) consecutive runs.
- **The VPS pages its own routes, if it can prove it.** An entrypoint failure is
  suppressed, with the reason recorded on `/status`, only when the production
  heartbeat is fresh, `schema: 2`, `ok` is not false, `failing_public_routes` lists
  the route (the runner lists only pages it delivered that are still active), and
  `last_delivery_ok_at` is no older than the last run that saw the entrypoint
  healthy (`since` minus one cron interval). Maintenance (an empty list), a route
  the runner never paged, an unhealthy loop, a stale delivery, or a v1 or stale
  heartbeat never suppress. Suppression is re-evaluated every run, so the Worker
  pages the moment it stops holding.
- **A v1 heartbeat's `ok` is unknown.** A v1 runner sends `ok=false` whenever any
  probe fails, so it neither fires nor resolves the loop P1. Deploy the v2 runner
  (#903) before this Worker.
- **Detection latency, measured** (`test_cloudflare_watchdog_kv_budget.py`, the
  VPS dying at each minute of a cron window): entrypoint page 31–60 min, stale
  heartbeat "VPS down" 82–111 min; the entrypoint page always comes first.
- **Alert state per failure identity** (`<env>:<name>:heartbeat-stale`,
  `:heartbeat-unhealthy`, `:entrypoint`). A still-active alert is re-sent at most
  every `WATCHDOG_RENOTIFY_SECONDS` (6 h); a changing detail is not a new alert;
  a recovery names what recovered and how long it was down. All events of one run
  go out as one message, directly to Feishu (never through the bridge it watches),
  with email as the fallback channel. The text uses the pager layout every source
  shares ([ops.observability.md §3.1](../../docs/ssot/ops.observability.md), #905):
  级别 · 环境 · 对象 · 现象 · 开始于 · 影响 · 下一步 · Runbook, the level taken from
  each target's or heartbeat's configured `severity` (`warning` is P2), times in
  UTC+8, and its own prose (summaries, recoveries, next steps) in Chinese with
  commands and identifiers verbatim.
- **Outage edges.** The production heartbeat's down and up transitions are kept
  (the last 50) for the weekly availability report, which cannot learn about the
  VPS's own downtime from the VPS: `start` is the last recorded contact, `end` the
  first fresh heartbeat after it.
- **An undelivered page is not marked sent**: the run fails, the next run retries.
- Malformed config (`WATCHDOG_TARGETS_JSON` / `WATCHDOG_HEARTBEATS_JSON`) or a
  missing KV binding pages as its own `config-preflight` failure and resolves
  nothing it could not evaluate.
- Sends a completion ping to Healthchecks.io after each run; a failed run sends
  `/fail`, a stopped cron produces a missed ping.

### Endpoints

| Route | Auth | Returns |
|---|---|---|
| `GET /health` | none | `{"ok": true}` |
| `POST /heartbeat` | `HEARTBEAT_TOKEN` | stores the runner's heartbeat (contract below) |
| `GET /status` | `WATCHDOG_STATUS_TOKEN` | the Worker's own health: `ok` = last run fresh and itself healthy (config valid, delivery succeeded) — not whether targets are up; plus active alerts, entrypoint streaks, both heartbeats and open outages |
| `GET /outages` | `WATCHDOG_STATUS_TOKEN` | `{"outages": [{"environment", "name", "start", "end", ...}]}`, epoch ms, `end: null` while open |

The GitHub daily audit reads `/status` for the Worker's liveness
(`tools/out_of_band_watchdog.py`); `tools/stability_report.py` reads `/outages`.

### Heartbeat contract v2

`POST /heartbeat` body: `env`, `name`, `ok`, `detail`, `timestamp`, optional
`liveness`, `"schema": 2`, `"last_delivery_ok_at"` (epoch seconds of the runner's
last 2xx from the alert bridge, 0 = none yet) and `"failing_public_routes"`
(sorted in-band public-route probe names failing in that loop). `ok` is the
probe loop's health, not "no probe failed". A payload without `schema` is v1: the
two new fields are stored as unknown (`null`), and so, for paging, is its `ok`. A
change of `ok` or of the failing route set is a verdict change under the write
budget below; a liveness ping never changes either. `detail` is stored cut to 300
characters; a route list longer than 32 names, or with a name over 64 characters,
is stored as unknown. `/status` serves at most 120 characters of any text field
and stays under 3 KB in the worst incident (GitHub reads 4096 bytes).

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
wrangler secret put WATCHDOG_DEADMAN_PING_URL
```

`HEARTBEAT_TOKEN` must match `INFRA_PROBE_HEARTBEAT_TOKEN` in the platform
alerting deployment. `WATCHDOG_STATUS_TOKEN` is required for authenticated
GitHub audit checks of `/status`.
`WATCHDOG_DEADMAN_PING_URL` is a separate Healthchecks.io check URL, stored only
as a Worker secret. Configure its period for 30 minutes and choose a grace and
notification route based on a controlled missed-ping drill. The notification
must not traverse this Worker, the VPS, SigNoz, or the internal alert bridge.

The Worker deployment workflow is manual because deployment applies to
Production. After owner approval of the exact reviewed `main` head SHA, dispatch
`Deploy Cloudflare Watchdog` on `main` with `approved_sha` set to that SHA. The
workflow rejects another actor, ref or SHA. Check the deployment job and the
external Healthchecks receipt before marking the dead-man switch live.

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

Deploying applies to production, so it is manual:
[`deploy-cloudflare-watchdog.yml`](../../.github/workflows/deploy-cloudflare-watchdog.yml)
runs only on `workflow_dispatch` from `main`, by the repository owner, with
`approved_sha` equal to the owner-approved `main` head. Merging does not deploy.
`tools/pr_merge_gate.py` treats these paths as deploy-triggering, so a PR here
needs the owner's approval of its head. `wrangler deploy --dry-run` validates the
bundle and config without auth.

### Observability (queryable logs)

`[observability]` is enabled in `wrangler.toml`, so `console.log` output is
persisted as Workers Logs and is queryable after the fact. Each cron run logs one
`watchdog.run` summary line (failing identities, suppressed entrypoints, fired /
renotified / resolved alerts, which KV key it wrote, any delivery error). Delivery
problems add `watchdog.delivery.*` events and heartbeat storage failures a
`watchdog.heartbeat.error` event.

### Budget (#904)

The Worker stays on the Workers Free plan. Measured by
`libs/tests/test_cloudflare_watchdog_kv_budget.py` and
`libs/tests/test_cloudflare_watchdog.py` (node, in-memory KV, fake network):

| Per cron run | Subrequests |
|---|---|
| quiet | 7 = 3 entrypoint GETs + production heartbeat read + state read + one write + dead-man ping |
| alert (Feishu app mode) | 9 = quiet + token + send |
| Feishu send fails after a token, email fallback | 10 |

Each run writes exactly one KV key: the state document (`watchdog:state`: alert
state, entrypoint streaks, outage edges, the run record) when anything
transitioned, otherwise the small `watchdog:last-run` record that keeps `/status`
fresh. CPU time cannot be measured locally; the target is cron CPU p99 < 5 ms on
the Cloudflare analytics after deployment.

### Free-quota safety

The worker must never trip the Cloudflare KV free-tier limit (1000 puts/day for
the whole account), because a quota trip silently freezes both heartbeat records:
every later heartbeat `put()` throws `KV put() limit exceeded for the day.` until
00:00 UTC, the records go stale, and the cron pages false heartbeat failures.

`recordHeartbeat` reads the stored record and writes only when
`heartbeatWrite()` says so:

- a **verdict** post (the runner's post-probe heartbeat) refreshes the record once
  per `WATCHDOG_HEARTBEAT_MIN_WRITE_INTERVAL_SECONDS` (`1800`; the worker uses `600`
  if it is unset, empty or non-numeric, and never less than `600` whatever it says —
  a NaN, zero or tiny interval would write every post);
- a **changed** verdict (`ok` or the failing route set) is written at once, up to
  `WATCHDOG_HEARTBEAT_STATUS_CHANGE_WRITES_PER_DAY` (`24`) times per key per UTC
  day; past that budget a flapping verdict waits for the next refresh;
- a **liveness** ping (`"liveness": true`, or the runner's legacy
  `probe loop iteration starting` detail) never changes the stored verdict; it only
  refreshes a record no verdict has refreshed for two intervals;
- only `(environment, name)` pairs in `WATCHDOG_HEARTBEATS_JSON` (for enabled
  environments) are stored; any other name gets HTTP 404 and costs nothing.

Worst-case puts per UTC day:

```
heartbeat keys * (ceil(86400 / interval) + status-change budget)   2 * (48 + 24) = 144
+ cron runs * 1                                                    48 * 1        =  48
                                                                                  = 192
```

A healthy day costs `2 * 48 + 48 = 144` (measured 140: 46 refreshes per key). Whatever the Worker env
says, the runtime clamps (interval ≥ 600 s, budget ≤ 24) cap it at
`2 * (144 + 24) + 48 = 384`. Before the liveness rule (2026-09-15/16) the liveness
ping's `ok=true` and a failing verdict alternated on every ~70 s probe loop and each
alternation was written: 1198 and 1157 puts/day. The daily ops check
(`tools/secrets_reconcile.py`) reports yesterday's `cloudflare.kv.write` against the
limit together with the last seven days and today so far. If KV `put()` still fails,
`recordHeartbeat` degrades to HTTP 200 with a logged `watchdog.heartbeat.error`
instead of an unhandled 500/1101.

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
  `production,staging`. Only `production` pages.
- `WATCHDOG_HTTP_TIMEOUT_MS`: per-entrypoint GET timeout, defaults to `8000`.
- `WATCHDOG_ENTRYPOINT_FAILURE_RUNS`: consecutive failing runs before an
  entrypoint pages, defaults to `2` (clamped to `1..6`).
- `WATCHDOG_RENOTIFY_SECONDS`: re-send interval of a still-active alert, defaults
  to `21600` (6 h).
- `WATCHDOG_STATUS_MAX_AGE_SECONDS`: `/status` calls the Worker stale past this,
  defaults to `7200`.
- `WATCHDOG_HEARTBEAT_MIN_WRITE_INTERVAL_SECONDS`: heartbeat refresh interval,
  defaults to `600` (`1800` in `wrangler.toml`).
- `WATCHDOG_HEARTBEAT_STATUS_CHANGE_WRITES_PER_DAY`: early writes of a changed
  verdict per heartbeat key per UTC day, defaults to `24` (also when empty or
  non-numeric) and is clamped to `0..24`.
- `WATCHDOG_TARGETS_JSON`: JSON array overriding the entrypoints. It must equal
  the built-in defaults and the registry's Cloudflare host-reachability signals
  (`docs/ssot/watchdog-signals.yaml`; tested).
- `WATCHDOG_HEARTBEATS_JSON`: JSON array overriding heartbeat checks.
- `ALERT_DELIVERY_MODE`: `feishu_webhook` or `feishu_app`.
- `FEISHU_APP_ID`: required for app bot mode.
- `FEISHU_CHAT_ID`: required for app bot mode.
- `FEISHU_API_BASE`: optional, defaults to `https://open.feishu.cn`.
