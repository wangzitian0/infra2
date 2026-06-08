# 告警 SSOT

> **SSOT Key**: `ops.alerting`
> **核心定义**: 定义告警规则、严重等级分级及飞书通知渠道。

---

## 1. 真理来源 (The Source)

本话题的配置和状态由以下物理位置唯一确定：

| 维度 | 物理位置 (SSOT) | 说明 |
|------|----------------|------|
| **规则定义** | **SigNoz Alert Manager** | 告警规则配置 |
| **通知渠道** | [platform/12.alerting](../../platform/12.alerting/) | SigNoz webhook → Feishu custom bot bridge |
| **通知密钥源头** | 1Password `platform/{env}/alerting` | Feishu webhook or app bot credentials, plus optional bridge basic auth |
| **运行时镜像** | Vault `secret/platform/{env}/alerting` | vault-agent 消费；由 `alerting.pre-compose` 从 1Password 同步 |

SigNoz webhook payloads use the Alertmanager schema. Feishu custom bot webhooks
require a `msg_type=text` payload, so SigNoz must target the internal bridge
endpoint instead of calling Feishu directly:

```text
SigNoz Alertmanager webhook
  -> http://platform-alerting${ENV_SUFFIX}:8080/signoz/webhook
  -> https://open.feishu.cn/open-apis/bot/v2/hook/<secret>
```

When custom webhooks are unavailable, the same bridge can use Feishu Open
Platform app bot mode:

```text
SigNoz Alertmanager webhook
  -> http://platform-alerting${ENV_SUFFIX}:8080/signoz/webhook
  -> Feishu OpenAPI /open-apis/im/v1/messages
```

Whole-host and alerting-stack failure detection is out-of-band because the
infra2 host can take SigNoz and `platform/12.alerting` down with it. The
primary out-of-band path is Cloudflare Workers Cron because it is external to
the VPS, low-cost at this probe volume, and does not consume GitHub Actions
minutes:

```text
Cloudflare Workers Cron (external to infra2, every 30 minutes)
  -> production and selected staging public route checks
  -> platform-alerting-probes heartbeat freshness checks
  -> Feishu/Lark webhook directly
```

The existing GitHub Actions watchdog remains a daily audit/manual diagnostic path
for Cloudflare Worker self-health and SSH-based host checks. All service-level
alerts continue to use the in-band path through SigNoz and `platform/12.alerting`.

Code-owned infra probes run from `platform/12.alerting` as
`platform-alerting-probes${ENV_SUFFIX}`. The probe runner checks service health
from inside the Dokploy network and posts SigNoz-compatible failure payloads to
the internal bridge:

```text
platform-alerting-probes${ENV_SUFFIX}
  -> HTTP/TCP/command probes
  -> http://platform-alerting${ENV_SUFFIX}:8080/signoz/webhook
  -> Feishu/Lark
```

Successful probe runs stay quiet. Failed probes emit
`InfraServiceProbeFailed` with the component name, probe kind, expected result,
and observed result.

The alert bridge must wait up to 300 seconds for `/secrets/.env` at startup, but
it must not require the vault-agent sidecar to remain Docker-healthy after the
secret file is rendered. Vault-agent stale-secret health is a separate
service-level signal; it must not block alert delivery.

---

## 2. 告警分级 (Severity)

| 等级 | 颜色 | 响应时效 | 定义 |
|------|------|----------|------|
| **P0 (Critical)** | 🔴 Red | 立即 (24x7) | 核心服务不可用 (Vault, SSO, DB Down) |
| **P1 (Error)** | 🟠 Orange | 30分钟 | 部分功能受损，核心链路仍通 |
| **P2 (Warning)** | 🟡 Yellow | 工作日 | 资源使用率高，非关键错误 |

## 3. Infra2 Alert Coverage Catalog

All in-band infra2 alert traffic must follow this path:

```text
component/app -> OpenTelemetry Collector -> SigNoz -> platform/12.alerting -> Feishu/Lark
```

| Layer | Component | Signal | Severity | Status |
|------|-----------|--------|----------|--------|
| L1 Bootstrap | 1Password Connect | `/health` is not active or sync is not active | P0 | Planned |
| L1 Bootstrap | Vault | sealed, unreachable, or token validation fails | P0 | Live via infra probe + vault audit |
| L1 Bootstrap | IaC Runner | `/health` fails before deployment webhook calls | P1 | Planned |
| L1 Bootstrap | Dokploy | deployment control-plane API/UI is unreachable or deployment webhooks fail; app health alerts remain app-owned | P1 | Live via infra probe |
| Cross-cutting | Docker container health | any running container is `unhealthy`, `health: starting`, or `Restarting` outside a deployment window | P0/P1 | Live via out-of-band watchdog SSH check |
| L2 Platform | platform Postgres | TCP readiness fails or restart loop | P0 | Live via infra probe |
| L2 Platform | platform Redis | TCP readiness fails or restart loop | P1 | Live via infra probe |
| L2 Platform | ClickHouse | `/ping` fails, disk pressure, or ingestion errors | P0 | Live via infra probe |
| L2 Platform | MinIO | MinIO live endpoint is unavailable | P1 | Live via infra probe |
| L2 Platform | Authentik | health endpoint fails | P0 | Live via infra probe |
| L2 Platform | SigNoz | frontend/query path fails | P0 | Live via infra probe |
| L2 Platform | Alert Bridge | `/health` fails or Feishu delivery errors | P0 | Live via infra probe; out-of-band bridge container health watchdog is also defined |
| L2 Platform | Portal | Homer frontend unavailable | P2 | Planned |
| L2 Platform | Activepieces | `/api/v1/flags` unavailable | P1 | Planned |
| L2 Platform | Prefect | server health port missing or worker stopped | P1 | Planned |
| L3 Finance | Wealthfolio | HTTP health check fails | P2 | Planned |
| L3 Finance Report | fr-postgres | app database health fails | P0 | Planned |
| L3 Finance Report | fr-redis | app cache health fails | P1 | Planned |
| L3 Finance Report | fr-app backend | OTEL ERROR/FATAL log count is above zero over 5 minutes | P1 | First live instance via shared rule automation |
| L3 Finance Report | fr-app public route | staging/production `report[-staging].zitian.party/` (web) or `/api/health` (API) fails from Cloudflare | P0 prod / P1 staging | Live via Cloudflare Workers out-of-band watchdog |
| L3 Finance Report | fr-app frontend | frontend HTTP health fails | P1 | Live via Cloudflare Workers out-of-band watchdog (public web route) |
| Cross-cutting | Vault app tokens and rendered env | missing, malformed, invalid, non-renewable, low TTL, or rendered `<no value>` fields | P0/P1 | Docker healthcheck + manual gate: `vault-audit.self-refresh` |
| Cross-cutting | Backup freshness | latest off-host backup is missing, stale, empty, or missing checksum | P1 | Live contract: backup manifest verifier |
| Cross-cutting | OTEL ingestion | expected app logs/traces absent after deployment | P1 | Manual gate: `signoz.shared.query-logs` |
| Cross-cutting | Infra2 host reachability | production or staging public infra2 endpoints fail from Cloudflare | P0/P1 | Cloudflare Workers out-of-band watchdog |
| Cross-cutting | Infra probe runner heartbeat | `platform-alerting-probes${ENV_SUFFIX}` stops posting heartbeat or reports unhealthy | P0/P1 | Cloudflare Workers out-of-band watchdog |
| Cross-cutting | SSH host diagnostics | external SSH bridge health check fails | P0 | GitHub Actions fallback watchdog |

---

## 4. 设计约束 (Dos & Don'ts)

### ✅ 推荐模式 (Whitelist)

- **模式 A**: 告警必须包含 Actionable 的信息（Runbook 链接）。
- **模式 B**: 尽量聚合告警，避免风暴。
- **模式 C**: 飞书 webhook 或 app secret 的长期源头只允许存放在 1Password，不允许写入 compose、README 或 Dokploy env；Vault 仅作为运行时镜像。
- **模式 D**: SigNoz webhook 只指向内部 bridge URL；飞书 URL/app secret 不暴露给 SigNoz channel。

### ⛔ 禁止模式 (Blacklist)

- **反模式 A**: **禁止** 为波动频繁的指标（如 CPU 瞬间峰值）设置 P0 告警。
- **反模式 B**: **禁止** 忽略 Critical 告警。
- **反模式 C**: **禁止** 将 SigNoz webhook channel 直接指向飞书自定义机器人。

---

## 5. 标准操作程序 (Playbooks)

### SOP-001: 响应 P0 告警

- **触发条件**: 收到 PagerDuty/电话通知
- **步骤**:
    1. 确认故障影响范围。
    2. 如果是基础设施故障，参考 [**Recovery SSOT**](./ops.recovery.md)。
    3. 在状态页更新 Incident。

### SOP-002: 接入飞书自定义机器人通知通道

1. 在飞书群中创建自定义机器人，复制 webhook URL。
2. 写入 1Password root vars:
   ```bash
   uv run invoke env.set FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/<token> --project=platform --env=production --service=alerting --credential-type=root_vars
   uv run invoke vault.setup-tokens --project=platform --service=alerting
   ```
3. 部署 bridge:
   ```bash
   uv run invoke alerting.setup
   uv run invoke alerting.status
   ```
4. 确保 SigNoz API key 存在，然后创建通知 channel:
   ```bash
   uv run invoke signoz.shared.create-api-key
   uv run invoke alerting.create-signoz-channel
   ```
5. 发送测试消息:
   ```bash
   uv run invoke alerting.test-feishu --message="Infra2 alert test"
   ```

### SOP-003: 接入飞书开发平台 App Bot 通知通道

1. 在飞书开放平台应用中启用机器人能力。
2. 申请并发布 `im:message:send_as_bot` 或 `im:message` 权限。
3. 将应用机器人添加到目标群，并获取该群 `chat_id`。
4. 写入 1Password root vars:
   ```bash
   uv run invoke env.set ALERT_DELIVERY_MODE=feishu_app --project=platform --env=production --service=alerting --credential-type=root_vars
   uv run invoke env.set FEISHU_APP_ID=cli_xxx --project=platform --env=production --service=alerting --credential-type=root_vars
   uv run invoke env.set FEISHU_APP_SECRET=<secret> --project=platform --env=production --service=alerting --credential-type=root_vars
   uv run invoke env.set FEISHU_CHAT_ID=<chat_id> --project=platform --env=production --service=alerting --credential-type=root_vars
   uv run invoke vault.setup-tokens --project=platform --service=alerting
   uv run invoke alerting.setup
   uv run invoke alerting.test-feishu --message="Infra2 alert test"
   ```

### SOP-004: 接入应用 OTEL 日志错误告警

1. Ensure the alert bridge is deployed and healthy:
   ```bash
   uv run python -m invoke vault.setup-tokens --project=platform --service=alerting
   uv run python -m invoke alerting.setup
   uv run python -m invoke alerting.status
   ```
2. Ensure the SigNoz API key exists:
   ```bash
   uv run python -m invoke signoz.shared.create-api-key
   ```
3. Create the internal Feishu channel and first app rule:
   ```bash
   uv run python -m invoke alerting.shared.ensure-log-error-rule \
     --alert-name=ExampleBackendErrorLogs \
     --service-name=example-backend
   ```
4. Send one synthetic bridge message as a live delivery gate:
   ```bash
   uv run python -m invoke alerting.shared.test-feishu --message="Finance report alerting path live"
   ```

### SOP-005: Cloudflare out-of-band infra2 watchdog

The primary out-of-band watchdog lives in
[`cloudflare/infra-watchdog`](../../cloudflare/infra-watchdog/). It runs every
30 minutes from Cloudflare Workers Cron and alerts Feishu directly instead of
routing through the bridge it is meant to verify.

Watchdog ownership is tracked per signal, not per component, in
[`watchdog-signals.yaml`](watchdog-signals.yaml). A component can have multiple
signals when they measure different failure domains; for example, MinIO internal
health is owned by the internal watchdog, while MinIO public-route health is
owned by the Cloudflare watchdog.

Default coverage:

- Production public routes:
  `cloud`, `vault`, `minio`, `sso`, and `signoz`.
- Staging public routes:
  `minio-staging`, `sso-staging`, and `signoz-staging`.
- Explicit staging public-route exclusions:
  `cloud-staging` and `vault-staging` while they return HTTP 404.
- Production and staging heartbeats:
  `platform-alerting-probes` and `platform-alerting-probes-staging`.

Required Cloudflare Worker secrets for webhook mode:

- `FEISHU_WEBHOOK_URL`: Feishu custom bot webhook URL.

Required Cloudflare Worker secrets for app bot mode:

- `FEISHU_APP_SECRET`: Feishu app secret.

Required Cloudflare Worker secrets for both delivery modes:

- `HEARTBEAT_TOKEN`: shared token expected by the `/heartbeat` endpoint.
- `WATCHDOG_STATUS_TOKEN`: shared token expected by the authenticated `/status`
  endpoint used by the GitHub audit watchdog.

1Password source of truth for the Worker API and status-check secrets:

- Vault: `Infra2`
- Item: `bootstrap/cloudflare-worker`
- Fields: `CLOUDFLARE_WORKER_API_TOKEN`, `WATCHDOG_STATUS_TOKEN`

CLI sync from 1Password to Cloudflare and GitHub:

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
  (cd cloudflare/infra-watchdog && \
    CLOUDFLARE_API_TOKEN="$worker_api_token" \
    wrangler secret put WATCHDOG_STATUS_TOKEN)

printf '%s' "$status_token" | \
  gh secret set INFRA2_WATCHDOG_WORKER_STATUS_TOKEN --repo wangzitian0/infra2

unset status_token worker_api_token
```

`env -u OP_SERVICE_ACCOUNT_TOKEN` intentionally bypasses a stale deleted service
account token when the local interactive `op` session is valid.

Required Cloudflare Worker KV:

- `WATCHDOG_STATE`: stores heartbeat timestamps and alert dedupe state.

Default Worker vars:

- `WATCHDOG_ENVIRONMENTS=production,staging`
- `WATCHDOG_HTTP_TIMEOUT_MS=8000`
- `WATCHDOG_RENOTIFY_SECONDS=7200`
- `WATCHDOG_STATUS_MAX_AGE_SECONDS=7200`
- `ALERT_DELIVERY_MODE=feishu_webhook` or `feishu_app`
- `FEISHU_APP_ID`: required for app bot mode
- `FEISHU_CHAT_ID`: required for app bot mode
- `FEISHU_API_BASE`: optional, defaults to `https://open.feishu.cn`

Deployment:

```bash
cd cloudflare/infra-watchdog
wrangler kv namespace create WATCHDOG_STATE
wrangler kv namespace create WATCHDOG_STATE --preview
wrangler secret put FEISHU_WEBHOOK_URL
wrangler secret put HEARTBEAT_TOKEN
wrangler secret put WATCHDOG_STATUS_TOKEN
wrangler deploy
```

Configure the in-band probe runner to publish heartbeat after deployment:

```bash
uv run invoke env.set INFRA_PROBE_HEARTBEAT_URL=https://infra2-cloudflare-watchdog.<account>.workers.dev/heartbeat --project=platform --env=production --service=alerting --credential-type=root_vars
uv run invoke env.set INFRA_PROBE_HEARTBEAT_TOKEN=<token> --project=platform --env=production --service=alerting --credential-type=root_vars
uv run invoke env.set INFRA_PROBE_HEARTBEAT_URL=https://infra2-cloudflare-watchdog.<account>.workers.dev/heartbeat --project=platform --env=staging --service=alerting --credential-type=root_vars
uv run invoke env.set INFRA_PROBE_HEARTBEAT_TOKEN=<token> --project=platform --env=staging --service=alerting --credential-type=root_vars
uv run invoke alerting.setup
DEPLOY_ENV=staging uv run invoke alerting.setup
```

The Worker is stateful. It sends an alert when a failure first appears, when the
failure fingerprint changes, when `WATCHDOG_RENOTIFY_SECONDS` is reached, and
when the previously failing watchdog recovers. Successful checks stay quiet. It
also writes `watchdog:last-run` to KV after scheduled runs so GitHub can detect
Worker cron or KV-backed state blindness; `/health` remains public and minimal,
while `/status` is bearer-token protected and returns only non-secret summary
state.

### SOP-005B: GitHub fallback out-of-band watchdog

The watchdog lives in GitHub Actions so it remains outside the infra2 host. It
runs daily and alerts Feishu directly instead of routing through the
bridge it is meant to verify. This path is retained for SSH-based host
diagnostics, Cloudflare Worker self-health audit, Dokploy route-canary liveness,
and manual dispatch.

Required GitHub secrets:

- `INFRA2_OUT_OF_BAND_ALERT_DELIVERY_MODE`: `feishu_webhook` or `feishu_app`
- `INFRA2_WATCHDOG_SSH_HOST`
- `INFRA2_WATCHDOG_SSH_USER`
- `INFRA2_WATCHDOG_SSH_PRIVATE_KEY`
- `INFRA2_WATCHDOG_WORKER_STATUS_TOKEN`
- `DOKPLOY_API_KEY`

For `feishu_webhook` mode:

- `INFRA2_OUT_OF_BAND_FEISHU_WEBHOOK_URL`

For `feishu_app` mode:

- `INFRA2_OUT_OF_BAND_FEISHU_APP_ID`
- `INFRA2_OUT_OF_BAND_FEISHU_APP_SECRET`
- `INFRA2_OUT_OF_BAND_FEISHU_CHAT_ID`
- `INFRA2_OUT_OF_BAND_FEISHU_API_BASE`: optional, defaults to `https://open.feishu.cn`

Required GitHub variables for Dokploy liveness:

- `DOKPLOY_ROUTE_CANARY_ENVIRONMENT_ID`
- `DOKPLOY_ROUTE_CANARY_PROJECT`: optional, defaults to `platform`
- `DOKPLOY_ROUTE_CANARY_ENV`: optional, defaults to `staging`
- `DOKPLOY_ROUTE_CANARY_HOST`: optional, defaults to a run-scoped
  `route-canary-watchdog-<run>.zitian.party` host
- `DOKPLOY_ROUTE_CANARY_DOKPLOY_HOST`: optional, defaults to
  `cloud.zitian.party`
- `DOKPLOY_ROUTE_CANARY_COMPOSE_NAME`: optional, defaults to
  `dokploy-route-canary-watchdog`
- `DOKPLOY_ROUTE_CANARY_TIMEOUT_SECONDS`: optional, defaults to `90`
- `DOKPLOY_ROUTE_CANARY_INTERVAL_SECONDS`: optional, defaults to `5`

The out-of-band watchdog treats missing Dokploy canary configuration as an
alert gap. When the canary classifies
`dokploy-worker-or-deployment-record`, the failure is reported through the
direct Feishu out-of-band route before application PR previews or staging
deploys spend time on app readiness.

Optional GitHub variables:

- `INFRA2_WATCHDOG_HTTP_TARGETS`: newline-separated `name|url|status_csv`
- `INFRA2_WATCHDOG_WORKER_STATUS_URL`: defaults to the deployed Worker
  `/status` endpoint.
- `INFRA2_WATCHDOG_SSH_TARGETS`: newline-separated `name|command|expected_text`
- `INFRA2_WATCHDOG_SSH_PORT`: defaults to `22`

Defaults check the public Dokploy entrypoint, Cloudflare Worker `/health`,
Cloudflare Worker authenticated `/status`, SSH reachability, Docker daemon
reachability, and the `platform-alerting` in-container `/health` endpoint via
SSH. IaC Runner, MinIO, Postgres, Redis, and application dependency probes are
service-level signals and remain in-band alerts owned by the bridge/SigNoz path.
Default SSH checks are mandatory: `INFRA2_WATCHDOG_SSH_TARGETS` can add checks or
override a check by name, but it must not remove `infra2-docker-health`. That
check fails on any Docker `unhealthy`, `health: starting`, or `Restarting`
container outside an active deployment window.

### SOP-006: Infra service probes

Infra service probes are configured in
[`platform/12.alerting/compose.yaml`](../../platform/12.alerting/compose.yaml)
under `INFRA_PROBE_SPECS`. The default probe loop interval is 60 seconds
(`INFRA_PROBE_INTERVAL_SECONDS=60`) for fast internal detection. Notification is
bounded separately: the default firing threshold is three consecutive failures
(`INFRA_PROBE_FAILURE_THRESHOLD=3`), the default recovery threshold is two
consecutive successes (`INFRA_PROBE_RECOVERY_THRESHOLD=2`), and unchanged active
failures renotify no more often than every 30 minutes
(`INFRA_PROBE_RENOTIFY_SECONDS=1800`).
In-band probes must prefer Docker-network targets for service health. Public
Cloudflare-routed domains are route checks, not core service-health checks, and
are primarily owned by the Cloudflare watchdog. If a future optional public-route
probe observes `error code: 1010`, it is classified as `probe-client-blocked`
rather than plain service down.

The probe runner is stateful. It sends an alert when a failure first appears,
when the failure fingerprint changes, when the configured renotify interval is
reached (`INFRA_PROBE_RENOTIFY_SECONDS`, default 1800 seconds), and when a
previously firing probe group recovers. Successful runs stay quiet unless they
close a previously active failure.

When `INFRA_PROBE_HEARTBEAT_URL` is configured, the probe runner posts a
heartbeat to the Cloudflare watchdog after each non-dry-run iteration. The
heartbeat payload includes the deploy environment, runner name, success flag,
detail, and local timestamp. Heartbeat delivery failures are logged but do not
block in-band probe alert delivery.

Public route probes live under `PUBLIC_ROUTE_PROBE_SPECS` and emit
`InfraPublicRouteProbeFailed` only when explicitly configured; service-health
probes emit `InfraServiceProbeFailed`. This keeps Cloudflare/Traefik route
failures separate from Docker-network service failures.

Spec format:

```text
name|kind|target|expected|severity|timeout_seconds
```

Supported kinds:

- `http`: expected is a comma-separated list of accepted HTTP status codes.
- `tcp`: expected is normally `connected`.
- `command`: expected is a substring of stdout.

Dry-run locally without sending Feishu:

```bash
INFRA_PROBE_DRY_RUN=1 uv run python tools/infra_probe_runner.py --once --json
```

### SOP-007: Dokploy dynamic route canary

Application PR previews must not be the first place that discovers platform
route materialization failures. The platform canary in
[`tools/dokploy_route_canary.py`](../../tools/dokploy_route_canary.py) deploys a
minimal two-service compose using the same routing shape as app previews: one
public web route and one higher-priority same-host `/api` route on
`dokploy-network`.

The canary fails fast by assigning failures to one of these domains:

- `dokploy-canary-configuration`: required canary configuration is missing, so
  the run did not prove the platform.
- `dokploy-control-plane`: compose create/update or deploy request failed.
- `dokploy-worker-or-deployment-record`: Dokploy accepted the request but no new
  `running`/`done` deployment record appeared.
- `docker-runtime`: expected containers or Traefik labels were not visible on
  the VPS when SSH inspection is configured.
- `traefik-public-route`: deployment and containers exist, but the public web
  and API routes did not both return 2xx/3xx.

Manual platform proof:

```bash
python tools/dokploy_route_canary.py \
  --host route-canary-$(date +%s).zitian.party \
  --environment-id="$DOKPLOY_ENVIRONMENT_ID" \
  --project platform \
  --env staging \
  --dokploy-host cloud.zitian.party
```

The `Dokploy Route Canary` GitHub workflow wraps the same tool for manual
operator runs, hourly scheduled proof, and main-branch changes to the canary
implementation. It requires `DOKPLOY_API_KEY`; scheduled and push runs also
read `DOKPLOY_ROUTE_CANARY_ENVIRONMENT_ID` from repository variables. Missing
environment configuration is a fail-closed `dokploy-canary-configuration`
result, never a skipped success, because an unconfigured scheduled canary cannot
protect app previews. Manual runs use the same rule unless `environment_id` is
provided as a workflow input or repository variable. SSH inspection is optional
and uses the existing watchdog SSH secrets when configured.

Every run writes a GitHub step summary with the canary status, failure domain,
compose ID, public URL, and each phase's evidence. App staging and preview gates
should treat a failing canary as a platform failure before spending time on
application readiness or browser E2E.
When Dokploy accepts `compose.deploy` but does not expose a deployment record,
the canary retries once with `compose.redeploy` before classifying the platform
as `dokploy-worker-or-deployment-record`.

---

## 6. 验证与测试 (The Proof)

| 行为描述 | 测试文件 (Test Anchor) | 覆盖率 |
|----------|-----------------------|--------|
| **Feishu payload contract** | `libs/tests/test_alerting.py` | ✅ Implemented |
| **Reusable SigNoz log error rule payload** | `libs/tests/test_alerting.py` | ✅ Implemented |
| **Out-of-band host and bridge watchdog contract** | `libs/tests/test_out_of_band_watchdog.py` | ✅ Implemented |
| **Cloudflare out-of-band watchdog contract** | `libs/tests/test_cloudflare_watchdog.py` | ✅ Implemented |
| **In-band infra service probes** | `libs/tests/test_infra_probes.py` | ✅ Implemented |
| **Dokploy dynamic route canary contract** | `libs/tests/test_dokploy_route_canary.py` | ✅ Implemented |
| **Backup freshness alert payload** | `libs/tests/test_backup_verification.py` | ✅ Implemented |
| **告警通道连通性** | `uv run invoke alerting.test-feishu` | Manual live gate |

---

## Used by

- [docs/ssot/README.md](./README.md)
- [docs/ssot/ops.observability.md](./ops.observability.md)
- [platform/12.alerting/README.md](../../platform/12.alerting/README.md)
