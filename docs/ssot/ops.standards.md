# 运维标准 SSOT

> **SSOT Key**: `ops.standards`
> **核心定义**: 定义基础设施的命名规范、标签策略、资源评估优先级及防御性运维守则。

---

## 1. 命名与标签规范

| 资源类型 | 格式 | 示例 |
|----------|------|------|
| **Namespace** | `<layer>[-<env>]` | `platform`, `data-staging` |
| **Service** | `<app>[-<role>]` | `redis-master`, `casdoor` |
| **Domain** | `<service>.<scope_domain>` | `sso.${INTERNAL_DOMAIN}` |

### 标准标签 (Tagging)
所有资源必须包含：`app.kubernetes.io/name`, `managed-by: terraform`, `part-of: cc-infra`。

---

## 2. 托管资源评估 SOP (Provider Priority)

> **原则**：优先使用成熟的、声明式的工具，最后才考虑脚本。

1.  **优先级**：原生/官方 Provider > 活跃社区 Provider > REST API Provider > `null_resource` > `local-exec`。
2.  **评估清单**：
    *   必须支持 `import` 块或 `data` source 探测。
    *   必须锁定版本 (`.terraform.lock.hcl`)。
    *   `null_resource` 必须幂等，且带明确的 `triggers` 和可验证输出。

---

## 3. 防御性运维守则 (Defensive Maintenance)

> **目的**：确保基础设施的可预测性和高置信度。

### Rule 1: 禁止黑盒参数 (No Blackbox Parameters)
使用新资源前，必须查阅官方 Registry。严禁猜测参数名（如猜测 `timeout` 或 `retry`）。

### Rule 2: 逻辑白盒化 (Whitebox Logic)
任何动态拼接的字符串（URL、ID、路径）必须在 Plan 输出中可见。
- **手段**：使用 `output` 或 `terraform_data` 打印结果以便审计。

### Rule 3: 漂移检测优先 (Drift Detection First)
针对外部 API 资源（Casdoor, Vault），优先使用 `data` source 配合 `precondition` 在 **Plan 阶段**检测冲突，而不是等到 Apply 报错。

### Rule 4: 状态不一致协议 (State Discrepancy Protocol)
如果 Apply 报错“资源已存在”，禁止盲目重试。
- **步骤**：1. 查询 Live API 确认状态；2. `terraform import` 同步状态；3. 必要时清理“幽灵资源”。

### Rule 5: 传播冷却 (Cooldown Period)
在部署 Ingress、DNS 或证书后，必须在健康检查前加入 `time_sleep`（建议 60s+），以应对解析延迟。

---

## 4. 变量与密钥链条守则

- **1Password 是真源**：禁止在 GitHub Web UI 修改 Secret。必须通过 `sync_secrets.py` 同步。
- **契约对齐**：修改 `variables.tf` 时必须同步更新 `tools/secrets/ci_load_secrets.py`。

---

## Used by

- [AGENTS.md](../../AGENTS.md)
- [docs/ssot/README.md](./README.md)
