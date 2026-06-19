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
| L1 Bootstrap | 1Password Connect | `/health` is not active or sync is not active | P0 | Live via infra probe (`op-connect-http`) |
| L1 Bootstrap | Vault | sealed, unreachable, or token validation fails | P0 | Live via infra probe + vault audit |
| L1 Bootstrap | IaC Runner | `/health` fails before deployment webhook calls | P1 | Live via infra probe (`iac-runner-http`) |
| L1 Bootstrap | Dokploy | deployment control-plane API/UI is unreachable or deployment webhooks fail; app health alerts remain app-owned | P1 | Live via infra probe |
| Cross-cutting | Docker container health | any running container is `unhealthy`, `health: starting`, or `Restarting` outside a deployment window | P0/P1 | Live via out-of-band watchdog SSH check |
| L2 Platform | platform Postgres | TCP readiness fails or restart loop | P0 | Live via infra probe |
| L2 Platform | platform Redis | TCP readiness fails or restart loop | P1 | Live via infra probe |
| L2 Platform | ClickHouse | `/ping` fails, disk pressure, or ingestion errors | P0 | Live via infra probe |
| L2 Platform | MinIO | MinIO live endpoint is unavailable | P1 | Live via infra probe |
| L2 Platform | Authentik | health endpoint fails | P0 | Live via infra probe |
| L2 Platform | SigNoz | frontend/query path fails or synthetic OTLP log nonce cannot be queried back from ClickHouse | P0 | Live via infra probes (`signoz-internal-http`, `otel-collector-http`, `signoz-roundtrip`) |
| L2 Platform | Alert Bridge | `/health` fails, Feishu host is unreachable, or low-frequency real-send proof fails | P0 | Live via infra probes (`alert-bridge-http`, `lark-delivery-http`, `alert-delivery-canary`); out-of-band bridge container health watchdog is also defined |
| L2 Platform | OpenPanel API | `/healthcheck` fails or synthetic `/track` event nonce cannot be queried back | P1 | Live via infra probes (`openpanel-api-http`, `openpanel-roundtrip`) |
| L2 Platform | OpenPanel ClickHouse | `/ping` fails (OpenPanel event store, separate from platform ClickHouse) | P1 | Live via infra probes (`openpanel-ch-http`, `openpanel-roundtrip`) |
| L2 Platform | OpenPanel Worker | `/healthcheck` fails (event processing queue worker) | P1 | Live via infra probe (`openpanel-worker-http`) |
| L2 Platform | OpenPanel Dashboard | `/api/healthcheck` fails (analytics UI) | P2 | Live via infra probe (`openpanel-dashboard-http`) |
| L2 Platform | Portal | Homer frontend unavailable | P2 | Planned |
| L2 Platform | Activepieces | `/api/v1/flags` unavailable | P1 | Planned |
| L2 Platform | Prefect | server health port missing or worker stopped | P1 | Planned |
| L3 Finance | Wealthfolio | HTTP health check fails | P2 | Planned |
| L3 Finance Report | fr-postgres | app database health fails | P0 | Planned |
| L3 Finance Report | fr-redis | app cache health fails | P1 | Planned |
| L3 Finance Report | fr-app backend | OTEL ERROR/FATAL log count is above zero over 5 minutes | P1 | Defined as code (`FinanceReportBackendErrorLogs`) in `finance_report/finance_report/observability/alert_rules.json`; applied via `fr-observability.shared.apply-alerts` |
| L3 Finance Report | fr-app backend | RED SLO signals: 5xx rate > 5% for 5m or p95 latency > 1500ms | P0/P1 | Defined as code (`FinanceReportHigh5xxRate`, `FinanceReportP95LatencyHigh`) in `finance_report/finance_report/observability/alert_rules.json`; applied via `fr-observability.shared.apply-alerts` |
| L3 Finance Report | fr-app backend | Business anomaly signals: parse failure spike, reconciliation anomaly, rate-limit saturation, async parse task failure | P1/P2 | Defined as code (`FinanceReportStatementParseFailureSpike`, `FinanceReportReconciliationAnomaly`, `FinanceReportRateLimitSaturation`, `FinanceReportAsyncTaskFailures`) in `finance_report/finance_report/observability/alert_rules.json`; applied via `fr-observability.shared.apply-alerts` |
| L3 Finance Report | fr-app public route | staging/production `report[-staging].zitian.party/` (web) or `/api/health` (API) fails from Cloudflare | P0 prod / P1 staging | Live via Cloudflare Workers out-of-band watchdog |
| L3 Finance Report | fr-app frontend | frontend HTTP health fails | P1 | Live via Cloudflare Workers out-of-band watchdog (public web route) |
| Cross-cutting | Vault app tokens and rendered env | missing, malformed, invalid, non-renewable, low TTL, or rendered `<no value>` fields | P0/P1 | Docker healthcheck + manual gate: `vault-audit.self-refresh` |
| Cross-cutting | Backup freshness | latest off-host backup is missing, stale, empty, or missing checksum | P1 | Live contract: backup manifest verifier |
| Cross-cutting | OTEL ingestion | expected app logs/traces absent after deployment | P1 | Infra synthetic path is live via `signoz-roundtrip`; app-specific post-deploy proof remains manual via `signoz.shared.query-logs` |
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
   uv run invoke vault.setup-approle --project=platform --service=alerting
   ```
3. 部署 bridge:
   ```bash
   uv run python -m tools.deploy_v2 --service platform/alerting --type prod --iac-ref vX.Y.Z --domain zitian.party --code-reviewed
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
   uv run invoke vault.setup-approle --project=platform --service=alerting
   uv run python -m tools.deploy_v2 --service platform/alerting --type prod --iac-ref vX.Y.Z --domain zitian.party --code-reviewed
   uv run invoke alerting.test-feishu --message="Infra2 alert test"
   ```

### SOP-004: 接入应用 OTEL 日志错误告警

1. Ensure the alert bridge is deployed and healthy:
   ```bash
   uv run python -m invoke vault.setup-approle --project=platform --service=alerting
   uv run python -m tools.deploy_v2 --service platform/alerting --type prod --iac-ref vX.Y.Z --domain zitian.party --code-reviewed
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

### SOP-004B: Apply finance_report alert + dashboard as code (#373)

The finance_report SigNoz objects are checked in under
[`finance_report/finance_report/observability/`](../../finance_report/finance_report/observability/):
`alert_rules.json` holds `FinanceReportBackendErrorLogs` (the alert name the app
references in `apps/backend/src/observability.py`) and `dashboard.json` holds the
baseline backend+frontend dashboard. Both are applied idempotently rather than
clicked into the UI. The rule routes through the shared internal bridge channel
(`infra2-feishu-alerts-<env>`) to Lark/Feishu; the Feishu webhook secret stays in
1Password `platform/{env}/alerting` and is mirrored to Vault
`secret/platform/{env}/alerting` at deploy time — it is never written into the
SigNoz channel or this repo.

1. Ensure the bridge, SigNoz API key, and Feishu channel exist (SOP-004 steps 1–3).
2. Apply the definitions:
   ```bash
   uv run python -m invoke fr-observability.shared.apply-alerts
   uv run python -m invoke fr-observability.shared.apply-dashboard
   ```
3. Inspect payloads offline without touching SigNoz:
   ```bash
   uv run python -m invoke fr-observability.shared.print-alerts
   uv run python -m invoke fr-observability.shared.print-dashboard
   ```
4. Post-merge live gate: emit a synthetic backend ERROR log, confirm
   `FinanceReportBackendErrorLogs` fires into the Lark group, and confirm the
   "Finance Report — Backend & Frontend" dashboard renders.

### SOP-004C: Apply finance_report SLO and business-anomaly alert catalog (#1106)

The finance_report alert catalog extends SOP-004B from one backend error-log rule
to the production runtime-hardening alert set under root `#1073`:

| Rule | Signal | Severity | Triage focus |
|------|--------|----------|--------------|
| `FinanceReportHigh5xxRate` | `http_server_request_count` 5xx rate > 5% for 5m | P0 | App outage or dependency failure; inspect deploy summary SigNoz pivots, backend logs, DB/Redis health, and recent release SHA. |
| `FinanceReportP95LatencyHigh` | `http_server_request_duration_bucket` p95 > 1500ms | P1 | Slow backend path; inspect RED dashboard by `deployment.environment`, DB pool gauges, and recent statement parse/provider load. |
| `FinanceReportStatementParseFailureSpike` | `finance_statement_parse_outcome{outcome=failed|rejected}` > 3 in 15m | P1 | Provider/parser regression; query by `statement_id`, `request_id`, model, and safe parse failure event. |
| `FinanceReportReconciliationAnomaly` | `finance_reconciliation_match_outcome{outcome=failed|error|anomaly}` > 0 in 15m | P1 | Reconciliation correctness risk; inspect reconciliation audit logs and recent matching code changes. |
| `FinanceReportRateLimitSaturation` | `finance_rate_limit_rejected` > 10 in 5m | P2 | Client burst or abuse; inspect auth/rate-limit warning logs before changing thresholds. |
| `FinanceReportAsyncTaskFailures` | `finance_async_parse_failure` > 0 in 5m | P1 | Silent background-work risk; inspect async task logs/spans and statement final state. |

Apply and inspect exactly like SOP-004B:

```bash
uv run python -m invoke fr-observability.shared.print-alerts
uv run python -m invoke fr-observability.shared.apply-alerts
```

The six metric rules must render as SigNoz v5 PromQL rules:
`alertType=METRIC_BASED_ALERT`, `ruleType=promql_rule`, and
`condition.compositeQuery.queries[]` with a `promql` query envelope. The current
production SigNoz API still expects numeric threshold enums (`op=1` for above,
`matchType=2` for all-times, etc.) even with the v5 query envelope. The apply task
must fail the process when SigNoz rejects any checked-in rule; a partial apply is
not a successful GitOps run.

Before reconciling the real catalog, run the workflow canary:

```bash
gh workflow run apply-observability.yml --repo wangzitian0/infra2 \
  --ref <branch-or-main> -f mode=canary
```

The canary creates one disabled temporary PromQL rule from the same payload
builder, verifies SigNoz stores the v5 `queries[]` envelope, and deletes the rule.

Merge/apply ordering: the config can be reviewed independently, but live apply
should happen after the app emits all referenced metric names. The rate-limit and
async-failure signals are introduced by the paired `#1107` and `#1108` app PRs;
rules for missing metrics stay harmless but cannot fire until those deploy.

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
- GitHub fallback watchdog coverage:
  public Dokploy entrypoint, Cloudflare Worker `/health`, Cloudflare Worker
  `/status`, Dokploy route canary, SSH reachability, Docker daemon reachability,
  and the `platform-alerting` in-container `/health` endpoint.
- Cloudflare Worker config-preflight failures are reported separately so invalid
  JSON or other config-parse errors do not masquerade as a route outage.
- Cloudflare alert dedupe keys on stable failure identity plus failure domain;
  volatile fields like heartbeat age stay in the alert body but do not create a
  new incident fingerprint every cron tick.
- Alert, watchdog, probe, and canary outputs that feed delivery decisions should
  use the shared Env x Stage contract in `libs/pipeline_stage_contract.py` so
  `stage`, `duration_ms`, `deadline_ms`, `failure_domain`,
  `external_dependency`, and suppression/skip reasons stay comparable.

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
uv run python -m tools.deploy_v2 --service platform/alerting --type prod --iac-ref vX.Y.Z --domain zitian.party --code-reviewed
uv run python -m tools.deploy_v2 --service platform/alerting --type staging --iac-ref vX.Y.Z --domain zitian.party
```

The Worker is stateful. It sends an alert when a failure first appears, when the
failure fingerprint changes, when `WATCHDOG_RENOTIFY_SECONDS` is reached, and
when the previously failing watchdog recovers. Successful checks stay quiet on
the alert channel. HTTP route checks retry using
`WATCHDOG_RETRY_MAX_ATTEMPTS`/`WATCHDOG_RETRY_DELAY_MS` before escalating. Each
scheduled run writes structured execution logs and `watchdog:last-run` to KV so
GitHub can detect Worker cron or KV-backed state blindness; `/health` remains
public and minimal, while `/status` is bearer-token protected and returns only
non-secret summary state. If delivery fails, the worker emits a
`watchdog.delivery.failure` structured event instead of failing silently.

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
- `DOKPLOY_ROUTE_CANARY_HOST`: optional, defaults to the stable
  `route-canary-watchdog.zitian.party` host
- `DOKPLOY_ROUTE_CANARY_DOKPLOY_HOST`: optional, defaults to
  `cloud.zitian.party`
- `DOKPLOY_ROUTE_CANARY_COMPOSE_NAME`: optional, defaults to
  `dokploy-route-canary-watchdog`
- `DOKPLOY_ROUTE_CANARY_TIMEOUT_SECONDS`: optional, defaults to `180`
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
- `INFRA2_WATCHDOG_RETRY_MAX_ATTEMPTS`: defaults to `2`.
- `INFRA2_WATCHDOG_RETRY_DELAY_SECONDS`: defaults to `60`.
- `INFRA2_WATCHDOG_SSH_TARGETS`: newline-separated `name|command|expected_text`
- `INFRA2_WATCHDOG_SSH_PORT`: defaults to `22`
- `INFRA2_WATCHDOG_ENABLE_FALLBACK_ISSUE`: defaults to `1`; if Feishu delivery
  fails, open a GitHub issue fallback.
- `INFRA2_WATCHDOG_FALLBACK_ISSUE_LABEL`: defaults to
  `watchdog-alert-fallback`.

Defaults check the public Dokploy entrypoint, Cloudflare Worker `/health`,
Cloudflare Worker authenticated `/status`, SSH reachability, Docker daemon
reachability, and the `platform-alerting` in-container `/health` endpoint via
SSH. IaC Runner, MinIO, Postgres, Redis, and application dependency probes are
service-level signals and remain in-band alerts owned by the bridge/SigNoz path.
When out-of-band Feishu delivery raises an exception, the watchdog emits a
`watchdog.delivery.failure` structured event, attempts GitHub fallback issue
creation (label `watchdog-alert-fallback`), and exits with failure so CI/logs
retain an auditable fallback signal.

Weekly digest is handled by `.github/workflows/watchdog-weekly-digest.yml`
(cron Monday UTC). It summarizes the last 7 days of
`out-of-band-watchdog.yml` workflow runs and reviews each run's structured
watchdog logs. The digest reports alert recall evidence:
`watchdog.delivery.success`, `watchdog.delivery.failure`, fallback issue URLs,
missing delivery evidence, and failure-domain counts. It then sends the compact
reliability digest to Feishu through the same direct delivery mode.

Current closure boundary:

- Closed-loop: public route watchdogs, probe-runner heartbeat, GitHub fallback
  watchdog checks, Dokploy route canary failures, deploy_v2 canary failures,
  SigNoz synthetic OTLP log round-trips, OpenPanel synthetic `/track`
  round-trips, alert bridge real-send canary failures, out-of-band Feishu
  delivery failures, and GitHub fallback issue creation are machine-audited and
  visible in weekly recall review.
- Known external limit: if every external channel used by the watchdog and
  Feishu/Lark delivery is unavailable at once, there is no third independent
  human-notification channel in this repo.
Default SSH checks are mandatory: `INFRA2_WATCHDOG_SSH_TARGETS` can add checks or
override a check by name, but it must not remove `infra2-docker-health`. That
check fails on any Docker `unhealthy`, `health: starting`, or `Restarting`
container outside an active deployment window.

### SOP-005C: Availability ledger and positive proof

Failure-only alerts cannot prove uptime, and the in-band SigNoz store cannot
measure its own host's availability (it shares the single VPS it would report
on). The availability ledger therefore lives **outside** the VPS: the Cloudflare
Worker records per-signal success + failure each run into Cloudflare KV (hot, 21
days) and archives finalized days to Cloudflare R2 (cold, long-term, the single
off-host store). `tools/stability_report.py` reads `/ledger` and sends Lark a
weekly positive-proof summary. Full contract, KV budget math, and the R2 vs
Google Drive decision are owned by
[`ops.availability-ledger.md`](./ops.availability-ledger.md). The weekly report
requires `INFRA2_WATCHDOG_LEDGER_URL`.

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

Synthetic closure probes are also part of `INFRA_PROBE_SPECS`:

- `signoz-roundtrip`: writes a synthetic OTLP log to the SigNoz collector, then
  queries `signoz_logs.distributed_logs_v2` in ClickHouse for the same nonce.
- `openpanel-roundtrip`: writes a synthetic OpenPanel `/track` event using the
  environment's finance client-id, then queries `openpanel.events` for the same
  nonce.
- `alert-delivery-canary`: posts a synthetic Alertmanager payload through the
  alert bridge to Feishu/Lark on a bounded cadence. Default interval is 6 hours
  (`ALERT_DELIVERY_CANARY_INTERVAL_SECONDS=21600`), so the probe proves real
  delivery without posting every minute.

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
- `dokploy-compose-source-type`: the canary compose is not using raw compose
  source, so Dokploy may route deployment through a Git provider instead of the
  rendered canary compose.
- `dokploy-worker-or-deployment-record`: Dokploy accepted the request but no new
  `running`/`done` deployment record appeared.
- `docker-runtime`: expected containers or Traefik labels were not visible on
  the VPS when SSH inspection is configured.
- `traefik-public-route`: deployment and containers exist, but the public web
  and API routes did not both return 2xx/3xx; public read timeouts are recorded
  as route probe evidence instead of crashing before summary output.

Manual platform proof:

```bash
python tools/dokploy_route_canary.py \
  --host route-canary.zitian.party \
  --environment-id="$DOKPLOY_ENVIRONMENT_ID" \
  --project platform \
  --env staging \
  --dokploy-host cloud.zitian.party
```

The `Dokploy Route Canary` GitHub workflow wraps the same tool for manual
operator runs, hourly scheduled proof, and main-branch changes to the canary
implementation. It requires `DOKPLOY_API_KEY`; scheduled and push runs also
read `DOKPLOY_ROUTE_CANARY_ENVIRONMENT_ID` from repository variables. By default
the GitHub workflow reuses `route-canary.zitian.party` with the stable
`dokploy-route-canary` compose, and the out-of-band watchdog reuses
`route-canary-watchdog.zitian.party` with `dokploy-route-canary-watchdog`. The
stable host/compose pairing prevents a fixed compose from carrying stale labels
for a different run-scoped host. Each workflow run still injects a non-sensitive
`infra2.route-canary.nonce` label so Dokploy must materialize a fresh deployment
record. Deployment record proof reads Dokploy's compose deployment listing API
first and uses the compose detail's embedded deployment snapshot only as a
compatibility fallback, because the compose detail payload can lag the runtime
deployment history. An accepted deploy/redeploy without a new record remains a
hard `dokploy-worker-or-deployment-record` failure unless the caller explicitly
enables stale canary repair. Stale repair is restricted to hosts whose slug
starts with `route-canary` and compose names whose slug starts with
`dokploy-route-canary`; it deletes the canary compose without volumes,
recreates it from the same rendered compose, immediately updates the recreated
compose back to `sourceType=raw`, and still requires a fresh deployment record
before probing Docker or public routes. Missing
environment configuration is a fail-closed `dokploy-canary-configuration`
result, never a skipped success, because an unconfigured scheduled canary cannot
protect app previews. Manual runs use the same rule unless `environment_id` is
provided as a workflow input or repository variable. SSH inspection is optional
and uses the existing watchdog SSH secrets when configured. The GitHub workflow
uses its concurrency group to serialize the stable canary compose per
project/environment; live run-scoped compose creation currently does not
reliably materialize Dokploy deployment records, so run-scoped names are not the
default guard path.

Every run writes a GitHub step summary with the canary status, failure domain,
compose ID, public URL, and each phase's evidence. App staging and preview gates
should treat a failing canary as a platform failure before spending time on
application readiness or browser E2E.
When Dokploy accepts `compose.deploy` but does not expose a deployment record,
the canary retries once with `compose.redeploy` before classifying the platform.
Deployment-record evidence includes non-secret compose source/status fields and
the latest deployment ID, status, log path, and truncated error message when
Dokploy exposes them. This keeps provider drift such as `Github Provider not
found` diagnosable from the workflow summary instead of requiring an immediate
VPS log dive.

---

## 6. 验证与测试 (The Proof)

| 行为描述 | 测试文件 (Test Anchor) | 覆盖率 |
|----------|-----------------------|--------|
| **Feishu payload contract** | `libs/tests/test_alerting.py` | ✅ Implemented |
| **Reusable SigNoz log error rule payload** | `libs/tests/test_alerting.py` | ✅ Implemented |
| **finance_report alert + dashboard config-as-code (#373)** | `libs/tests/test_observability_dashboards.py` | ✅ Implemented |
| **Out-of-band host and bridge watchdog contract** | `libs/tests/test_out_of_band_watchdog.py` | ✅ Implemented |
| **Cloudflare out-of-band watchdog contract** | `libs/tests/test_cloudflare_watchdog.py` | ✅ Implemented |
| **In-band infra service probes** | `libs/tests/test_infra_probes.py` | ✅ Implemented |
| **Dokploy dynamic route canary contract** | `libs/tests/test_dokploy_route_canary.py` | ✅ Implemented |
| **Backup freshness alert payload** | `libs/tests/test_backup_verification.py` | ✅ Implemented |
| **Availability ledger aggregation (正例+反例)** | `libs/tests/test_availability_ledger.py` | ✅ Implemented |
| **Worker ledger + `/ledger` + R2 archive contract** | `libs/tests/test_cloudflare_watchdog.py` | ✅ Implemented |
| **Weekly watchdog recall digest** | `libs/tests/test_watchdog_weekly_digest.py` | ✅ Implemented |
| **Weekly positive stability report** | `libs/tests/test_stability_report.py` | ✅ Implemented |
| **Env x Stage failure-domain and disagreement contract** | `libs/tests/test_pipeline_stage_contract.py` | ✅ Implemented |
| **SigNoz/OpenPanel synthetic round-trip probes** | `libs/tests/test_observability_roundtrip_probe.py` | ✅ Implemented |
| **告警通道真实投递 canary** | `libs/tests/test_alert_delivery_canary.py` | ✅ Implemented |
| **告警通道手动连通性** | `uv run invoke alerting.test-feishu` | Manual live gate |

---

## Used by

- [docs/ssot/README.md](./README.md)
- [docs/ssot/ops.observability.md](./ops.observability.md)
- [platform/12.alerting/README.md](../../platform/12.alerting/README.md)
