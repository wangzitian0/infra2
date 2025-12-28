# 运维标准 SSOT

> **SSOT Key**: `ops.standards`
> **核心定义**: 定义基础设施的命名规范、标签策略、资源评估优先级及防御性运维守则。

---

## 1. 命名与标签规范

| 资源类型 | 格式 | 示例 |
|----------|------|------|
| **Service Directory** | `{nn}.{service}` | `01.postgres`, `10.authentik` |
| **Dokploy App** | `<service>` | `postgres`, `redis` |
| **Container** | `<scope>-<service>[-<role>]` | `platform-postgres`, `authentik-server` |
| **Domain** | `<service>.<internal_domain>` | `sso.${INTERNAL_DOMAIN}` |

### 标准标签 (Tagging)
- 对外服务必须配置 Traefik labels（`traefik.enable`, router rule, entrypoints, tls）。
- 内部服务必须显式 `traefik.enable=false`，避免误暴露。

---

## 2. 托管资源评估 SOP (Provider Priority)

> **原则**：优先使用官方工具与可观察的流程，最后才考虑临时脚本。

1.  **优先级**：官方 CLI/SDK > Dokploy UI/CLI > 可复用脚本 > 手工操作。
2.  **评估清单**：
    *   操作必须可重复（幂等或可安全重跑）。
    *   必须有可验证输出（`invoke` 返回结果 / Dokploy 健康状态）。
    *   所有环境变量入口必须有 `.env.example` 可追溯。

---

## 3. 防御性运维守则 (Defensive Maintenance)

> **目的**：确保基础设施的可预测性和高置信度。

### Rule 1: 禁止黑盒参数 (No Blackbox Parameters)
使用新资源前，必须查阅官方 Registry。严禁猜测参数名（如猜测 `timeout` 或 `retry`）。

### Rule 2: 逻辑白盒化 (Whitebox Logic)
任何动态拼接的字符串（URL、ID、路径）必须在任务输出中可见。
- **手段**：使用 `libs.console.env_vars` 或在 README 中显式列出。

### Rule 3: 漂移检测优先 (Drift Detection First)
针对外部服务（Vault, Authentik），优先在部署前执行 `invoke <service>.shared.status` 或 Dokploy 健康检查，避免在运行期才暴露问题。

### Rule 4: 状态不一致协议 (State Discrepancy Protocol)
如果部署过程中出现“资源已存在/状态不一致”，禁止盲目重试。
- **步骤**：1. 查询 Dokploy 应用状态；2. `docker ps`/日志确认实际运行；3. 必要时手动清理并更新 SSOT。

### Rule 5: 传播冷却 (Cooldown Period)
在部署 DNS 或证书后，必须在健康检查前加入等待窗口（建议 60s+），以应对解析延迟。

---

## 4. 变量与密钥链条守则

- **1Password 是真源**：禁止在 Web UI 临时改密钥，必须记录在 1Password。
- **Vault 是运行时真理**：服务部署必须从 Vault 读写或显式填写。
- **契约对齐**：调整环境变量时，更新 `.env.example` 与对应服务 README。

---

## Used by

- [AGENTS.md](https://github.com/wangzitian0/infra2/blob/main/AGENTS.md)
- [docs/ssot/README.md](./README.md)
