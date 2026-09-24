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
- 服务身份只有一个版本化契约：`libs/service_identity.py` 的 `v1`。canonical
  坐标为 `service_id=<namespace>/<service>`、`environment`、`component`，发布
  坐标为 `service.version` / `iac_ref`；`managed_by=infra2` 标识签发者。
- IaC/Dokploy 使用 `INFRA_*`，OpenTelemetry 使用 semantic conventions
  (`service.namespace`, `service.name`, `service.version`,
  `deployment.environment.name`)，告警使用对应 snake_case 低基数 labels，Docker
  显式标签使用 `party.zitian.infra.*`。迁移期只允许额外双写旧
  `deployment.environment`，禁止另建服务名映射。
- Docker 观察器优先读取 `party.zitian.infra.*`；旧容器只可由 Compose 自动标签和
  registry 解析，无法唯一解析必须标为 `infra/unregistered`，不得猜测归属。
- `service_id/environment/component/severity/failure_domain` 可参与告警路由和去重；
  container ID、nonce、时间戳、错误详情只能放 annotations/log body，禁止成为 label。

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
只读观测必须保留三态：“有数据”、“确认为空”、“观测失败/未知”。API、认证或
传输失败不得转成空集合后继续计算 drift；应使检测段失败，但不伪造 confirmed finding。

### Rule 4: 状态不一致协议 (State Discrepancy Protocol)
如果部署过程中出现“资源已存在/状态不一致”，禁止盲目重试。
- **步骤**：1. 查询 Dokploy 应用状态；2. `docker ps`/日志确认实际运行；3. 必要时手动清理并更新 SSOT。
- **交叉判定**（GitHub 日审计的 Dokploy 状态族，#908）：Dokploy `error` + 同一运行里独立
  Docker health 绿，或该单元最新部署记录超过 72h = **过期记录**，不算失败，但仍列入日报
  供对账，先对账不盲目重启；Dokploy `error` + Docker health 红/未知且记录在 72h 内 =
  仍是失败（只报告，不 page）。任一平面的绿不得把另一平面的红静默掉：过期记录照样送达。

### Rule 5: 传播冷却 (Cooldown Period)
在部署 DNS 或证书后，必须在健康检查前加入等待窗口（建议 60s+），以应对解析延迟。

### Rule 6: 镜像必须钉版本 (Pin Images)
platform compose 中的镜像禁止浮动 tag——上游静默漂移且不可复现。事故先例：#253/#255 的 prefect 卡死源于浮动的 `:3-latest` tag 漂移。规范要求钉到不随上游移动的引用：至少钉具体版本，推荐钉 digest（`image: repo:tag@sha256:...`）。
- **机械守卫**（范围小于规范本身）：`tools/lint_platform_image_pins.py`（infra-ci 阻断）目前只拦截字面量裸 `:latest`；其它浮动 tag（如 `:3-latest`、`:stable`）靠 review 把关。逻辑在 `libs/image_pins.py`，proof `libs/tests/test_image_pins.py`。

### Rule 7: 发布前 Schema 与枚举双向校验 (Pre-Deploy Schema Gate, #698)
在任何涉及持久化存储的应用部署销毁或重启容器前，必须执行 Pre-Deploy Schema Gate（`tools/pre_deploy_schema_check.py`）：
- **双向严格比对**：代码缺失 DB 已有枚举（`missing_in_code`）或 DB 缺失代码枚举（`missing_in_db`）均视为不兼容阻断项。
- **Fail-Closed 原则**：数据库查询失败、连接超时或解析异常一律视为不满足，强制中断发布流程。
- **未评估即阻断（#718 review）**：没有数据库 URL、服务未登记代码侧枚举来源（`ENUM_SOURCES`）或该来源导入失败/没有任何原生枚举，均输出 `NOT EVALUATED` 并以退出码 3 阻断——“什么都没比”绝不等于“0 处不一致”。退出码：0 通过 · 1 不一致或 DB 失败 · 3 未评估。
- **代码侧真源**：服务自己的 SQLAlchemy metadata（finance_report：`import src.orm_registry` 后的 `src.database:Base.metadata`，Postgres 枚举类型名取自 `Enum(name=...)`），因此门禁在应用自己的 Python 环境里运行（`--app-path <finance_report checkout>/apps/backend`）。当前尚未接入 `deploy_v2`（见 Infra-022 TODOWRITE）。
- **事故先例**：2026-09-10 事故（#698）中，因 Enum 大小写不匹配（'PENDING' vs 'pending'）导致 finance_report 后端容器重启即崩溃死循环，服务中断 13 分钟。

### Rule 8: 写入非幂等禁止盲目重试与幂等键铁律 (Safe Retries & Idempotency)
网络抖动重试仅限幂等操作：
- **安全重试**：仅只读 GET 及具备状态收敛性质的幂等写（如 `compose.update` 覆盖声明）允许在 502/503/504 时退避重试。
- **禁止盲目重试**：所有创建、部署触发、资源删除（非幂等 POST/DELETE）在发生请求超时或无响应时，**严禁**盲目发起重试；若需重试，必须由客户端携带 `Idempotency-Key` 并在服务端做防重校验，杜绝幽灵重复部署。

---

## 4. 变量与密钥链条守则

- **1Password 是真源**：禁止在 Web UI 临时改密钥，必须记录在 1Password。
- **Vault 是运行时真理**：服务部署必须从 Vault 读写或显式填写。
- **滚动重启生效（Rolling Restart）**：密钥轮转不具备进程内存级零停机热加载能力，密码/凭据更新必须通过触发容器重建（`reconcile-iac-inputs.yml` 或 `deploy_v2` 重建）生效。
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
