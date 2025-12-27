# 告警 SSOT

> **SSOT Key**: `ops.alerting`
> **核心定义**: 定义告警规则、严重等级分级及通知渠道。

---

## 1. 真理来源 (The Source)

本话题的配置和状态由以下物理位置唯一确定：

| 维度 | 物理位置 (SSOT) | 说明 |
|------|----------------|------|
| **规则定义** | **SigNoz Alert Manager** | 告警规则配置 |
| **通知渠道** | **Slack / Email** | 接收端 |

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

### ⛔ 禁止模式 (Blacklist)

- **反模式 A**: **禁止** 为波动频繁的指标（如 CPU 瞬间峰值）设置 P0 告警。
- **反模式 B**: **禁止** 忽略 Critical 告警。

---

## 4. 标准操作程序 (Playbooks)

### SOP-001: 响应 P0 告警

- **触发条件**: 收到 PagerDuty/电话通知
- **步骤**:
    1. 确认故障影响范围。
    2. 如果是基础设施故障，参考 [**Recovery SSOT**](./ops.recovery.md)。
    3. 在状态页更新 Incident。

---

## 5. 验证与测试 (The Proof)

| 行为描述 | 测试文件 (Test Anchor) | 覆盖率 |
|----------|-----------------------|--------|
| **告警通道连通性** | `test_alert_channel.py` (Pending) | ⏳ Planned |

---

## Used by

- [docs/ssot/README.md](./README.md)
- [docs/ssot/ops.observability.md](./ops.observability.md)