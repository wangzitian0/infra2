# 运维标准 SSOT

> **SSOT Key**: `ops.standards`
> **核心定义**: 定义基础设施的命名规范、标签策略、资源评估优先级及防御性运维守则。

---

## 1. 命名与标签规范

| 资源类型 | 格式 | 示例 |
|----------|------|------|
| **Service Directory** | `{nn}.{service}` | `01.postgres`, `10.authentik` |
| **Dokploy App** | `<service>` | `postgres`, `redis` |
| **Container** | `<scope>-<service>[-<role>]${ENV_SUFFIX}` | `platform-postgres${ENV_SUFFIX}`, `authentik-server${ENV_SUFFIX}` |
| **Domain** | `<service>${ENV_DOMAIN_SUFFIX}.<internal_domain>` | `sso${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}` |

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

## 5. 资源治理 (Resource Governance)

> **核心**: 单台 VPS 上 prod / staging / preview / playground 共存。**每个容器必须声明资源限额**，否则一个失控/泄漏的容器能吃爆整机（实证:2026-06 prefect 占 3G、零限额下内存 27/31G、Dokploy 控制面超时）。CI 不在本机（GitHub runner），不参与本机配额。

### 5.1 隔离原则

隔离的轴 = 风险的轴。**控制/平台面**（Dokploy/Vault/SigNoz/Authentik/MinIO/Prefect…）有内建多租户 → 一份 + HA + 逻辑租户即可；**数据/工作负载面**（app 容器 + 其逻辑库/bucket）→ per-(env×app) 隔离。staging 与 prod **故意两套**(staging 是升级/IaC 变更的金丝雀,挂了也比 prod 直接挂强)，但 staging 跑**最低资源 tier**。

### 5.2 资源 tier(两个独立旋钮:预算 ≠ 优先级)

| tier | `mem_limit`(天花板) | `mem_reservation`(保底) | `cpu_shares`(竞争权重) | OOM 取舍 |
|------|--------------------|------------------------|----------------------|----------|
| **prod** | 充足,**整机 ≤ 50%** | 设(保证 floor) | 1024 | 最后(受保护) |
| **staging** | **最低,够跑即可** | 不设 | 512 | 中 |
| **preview** | 紧,临时 | 不设 | 256 | 早 |
| **playground** | 宽松上限(防吃爆) | 不设 | 128 | 最早 |

要点:
- **天花板必须设在观测峰值之上**——限到峰值以下会触发 OOM 重启循环,比不限更糟。先给带余量的天花板防失控,观察后再收紧。
- **prod ≤ 50% 是封顶**(防 prod 泄漏吃爆全机),配 `reservation` 保底;overcommit(各 tier 上限之和 > 100%)允许,只要 **reservation 之和 ≤ 整机**。
- `cpu_shares` 是相对权重,**零 OOM 风险**,可放心按 tier 设。

### 5.3 落地约定

- 限额写进 **compose 字段**(`mem_limit`/`mem_reservation`/`cpu_shares`),由 env 变量驱动、prod 默认 baked、staging/preview 部署时 override 到更低:
  ```yaml
  mem_limit: ${SVC_MEM_LIMIT:-<prod天花板>}
  cpu_shares: ${TIER_CPU_SHARES:-1024}   # staging 部署设 512, preview 256
  ```
- **白名单**: 新增服务的 compose **必须**带 `mem_limit` + `cpu_shares`。
- **黑名单**: 禁止无限额服务上 prod;禁止把天花板设到观测峰值以下。

---

## Used by

- [AGENTS.md](https://github.com/wangzitian0/infra2/blob/main/AGENTS.md)
- [docs/ssot/README.md](./README.md)
- [docs/ssot/ops.observability.md](./ops.observability.md)
