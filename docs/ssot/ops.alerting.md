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
infra2 host can take SigNoz and `platform/12.alerting` down with it:

```text
GitHub Actions schedule (external to infra2, every 30 minutes)
  -> public infra2 endpoint checks
  -> SSH bridge container health check
  -> Feishu/Lark webhook directly
```

This path is intentionally limited to host reachability and alerting bridge
availability. All service-level alerts continue to use the in-band path through
SigNoz and `platform/12.alerting`.

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
| L1 Bootstrap | Vault | sealed, unreachable, or token validation fails | P0 | Planned |
| L1 Bootstrap | IaC Runner | `/health` fails before deployment webhook calls | P1 | Planned |
| L1 Bootstrap | Dokploy | deployment control-plane API/UI is unreachable or deployment webhooks fail; app health alerts remain app-owned | P1 | Planned |
| L2 Platform | platform Postgres | `pg_isready` fails or restart loop | P0 | Planned |
| L2 Platform | platform Redis | `redis-cli ping` fails or restart loop | P1 | Planned |
| L2 Platform | ClickHouse | `/ping` fails, disk pressure, or ingestion errors | P0 | Planned |
| L2 Platform | MinIO | `mc ready local` fails or S3 endpoint is unavailable | P1 | Planned |
| L2 Platform | Authentik | `ak healthcheck` fails | P0 | Planned |
| L2 Platform | SigNoz | query-service or OTEL collector health fails | P0 | Planned |
| L2 Platform | Alert Bridge | `/health` fails or Feishu delivery errors | P0 | Planned; out-of-band bridge container health watchdog is defined |
| L2 Platform | Portal | Homer frontend unavailable | P2 | Planned |
| L2 Platform | Activepieces | `/api/v1/flags` unavailable | P1 | Planned |
| L2 Platform | Prefect | server health port missing or worker stopped | P1 | Planned |
| L3 Finance | Wealthfolio | HTTP health check fails | P2 | Planned |
| L3 Finance Report | fr-postgres | app database health fails | P0 | Planned |
| L3 Finance Report | fr-redis | app cache health fails | P1 | Planned |
| L3 Finance Report | fr-app backend | OTEL ERROR/FATAL log count is above zero over 5 minutes | P1 | First live instance via shared rule automation |
| L3 Finance Report | fr-app frontend | frontend HTTP health fails | P1 | Planned |
| Cross-cutting | Vault app tokens | missing, malformed, invalid, non-renewable, or low TTL | P0/P1 | Manual gate: `vault-audit.self-refresh` |
| Cross-cutting | OTEL ingestion | expected app logs/traces absent after deployment | P1 | Manual gate: `signoz.shared.query-logs` |
| Cross-cutting | Infra2 host reachability | public infra2 endpoints or external SSH bridge health check fail | P0 | GitHub Actions out-of-band watchdog |

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

### SOP-005: Out-of-band infra2 watchdog

The watchdog lives in GitHub Actions so it remains outside the infra2 host. It
runs every 30 minutes and alerts Feishu directly instead of routing through the
bridge it is meant to verify.

Required GitHub secrets:

- `INFRA2_OUT_OF_BAND_ALERT_DELIVERY_MODE`: `feishu_webhook` or `feishu_app`
- `INFRA2_WATCHDOG_SSH_HOST`
- `INFRA2_WATCHDOG_SSH_USER`
- `INFRA2_WATCHDOG_SSH_PRIVATE_KEY`

For `feishu_webhook` mode:

- `INFRA2_OUT_OF_BAND_FEISHU_WEBHOOK_URL`

For `feishu_app` mode:

- `INFRA2_OUT_OF_BAND_FEISHU_APP_ID`
- `INFRA2_OUT_OF_BAND_FEISHU_APP_SECRET`
- `INFRA2_OUT_OF_BAND_FEISHU_CHAT_ID`
- `INFRA2_OUT_OF_BAND_FEISHU_API_BASE`: optional, defaults to `https://open.feishu.cn`

Optional GitHub variables:

- `INFRA2_WATCHDOG_HTTP_TARGETS`: newline-separated `name|url|status_csv`
- `INFRA2_WATCHDOG_SSH_TARGETS`: newline-separated `name|command|expected_text`
- `INFRA2_WATCHDOG_SSH_PORT`: defaults to `22`

Defaults check the public Dokploy entrypoint, SSH reachability, Docker daemon
reachability, and the `platform-alerting` in-container `/health` endpoint via
SSH. IaC Runner, MinIO, Postgres, Redis, and application dependency probes are
service-level signals and remain in-band alerts owned by the bridge/SigNoz path.

---

## 6. 验证与测试 (The Proof)

| 行为描述 | 测试文件 (Test Anchor) | 覆盖率 |
|----------|-----------------------|--------|
| **Feishu payload contract** | `libs/tests/test_alerting.py` | ✅ Implemented |
| **Reusable SigNoz log error rule payload** | `libs/tests/test_alerting.py` | ✅ Implemented |
| **Out-of-band host and bridge watchdog contract** | `libs/tests/test_out_of_band_watchdog.py` | ✅ Implemented |
| **告警通道连通性** | `uv run invoke alerting.test-feishu` | Manual live gate |

---

## Used by

- [docs/ssot/README.md](./README.md)
- [docs/ssot/ops.observability.md](./ops.observability.md)
- [platform/12.alerting/README.md](../../platform/12.alerting/README.md)
