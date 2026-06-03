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
| **通知密钥** | `secret/platform/{env}/alerting` | `FEISHU_WEBHOOK_URL` and optional bridge basic auth |

SigNoz webhook payloads use the Alertmanager schema. Feishu custom bot webhooks
require a `msg_type=text` payload, so SigNoz must target the internal bridge
endpoint instead of calling Feishu directly:

```text
SigNoz Alertmanager webhook
  -> http://platform-alerting${ENV_SUFFIX}:8080/signoz/webhook
  -> https://open.feishu.cn/open-apis/bot/v2/hook/<secret>
```

---

## 2. 告警分级 (Severity)

| 等级 | 颜色 | 响应时效 | 定义 |
|------|------|----------|------|
| **P0 (Critical)** | 🔴 Red | 立即 (24x7) | 核心服务不可用 (Vault, SSO, DB Down) |
| **P1 (Error)** | 🟠 Orange | 30分钟 | 部分功能受损，核心链路仍通 |
| **P2 (Warning)** | 🟡 Yellow | 工作日 | 资源使用率高，非关键错误 |

---

## 3. 设计约束 (Dos & Don'ts)

### ✅ 推荐模式 (Whitelist)

- **模式 A**: 告警必须包含 Actionable 的信息（Runbook 链接）。
- **模式 B**: 尽量聚合告警，避免风暴。
- **模式 C**: 飞书 webhook 只允许存放在 Vault，不允许写入 compose、README 或 Dokploy env。
- **模式 D**: SigNoz webhook 只指向内部 bridge URL；飞书 URL 不暴露给 SigNoz channel。

### ⛔ 禁止模式 (Blacklist)

- **反模式 A**: **禁止** 为波动频繁的指标（如 CPU 瞬间峰值）设置 P0 告警。
- **反模式 B**: **禁止** 忽略 Critical 告警。
- **反模式 C**: **禁止** 将 SigNoz webhook channel 直接指向飞书自定义机器人。

---

## 4. 标准操作程序 (Playbooks)

### SOP-001: 响应 P0 告警

- **触发条件**: 收到 PagerDuty/电话通知
- **步骤**:
    1. 确认故障影响范围。
    2. 如果是基础设施故障，参考 [**Recovery SSOT**](./ops.recovery.md)。
    3. 在状态页更新 Incident。

### SOP-002: 接入飞书通知通道

1. 在飞书群中创建自定义机器人，复制 webhook URL。
2. 写入 Vault:
   ```bash
   uv run invoke env.set FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/<token> --project=platform --env=production --service=alerting
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

---

## 5. 验证与测试 (The Proof)

| 行为描述 | 测试文件 (Test Anchor) | 覆盖率 |
|----------|-----------------------|--------|
| **Feishu payload contract** | `libs/tests/test_alerting.py` | ✅ Implemented |
| **告警通道连通性** | `uv run invoke alerting.test-feishu` | Manual live gate |

---

## Used by

- [docs/ssot/README.md](./README.md)
- [docs/ssot/ops.observability.md](./ops.observability.md)
- [platform/12.alerting/README.md](../../platform/12.alerting/README.md)
