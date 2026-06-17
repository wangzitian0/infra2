# OpenPanel SSOT

> **SSOT Key**: `platform.openpanel`
> **核心定义**：OpenPanel 是一款开源产品分析平台。其在 Dokploy 中的部署复用共享的 PostgreSQL（配置/账户）与 Redis（队列/缓存），但运行**专属、版本匹配的 ClickHouse**（事件存储），并与 Vault 集成以确保密钥安全。
>
> **镜像契约**：使用上游官方镜像 `lindesvard/openpanel-{api,dashboard,worker}:2`。`op-api` 监听 `:3000`，启动时必须先执行 DB 迁移（`pnpm -r run migrate:deploy`）再 `pnpm start`。环境变量遵循上游 self-hosting 约定（`DATABASE_URL`/`DATABASE_URL_DIRECT`/`REDIS_URL`/`CLICKHOUSE_URL`/`COOKIE_SECRET`/`API_URL`/`DASHBOARD_URL`/`ALLOW_REGISTRATION` 等）。

---

## 1. 真理来源 (The Source)

| 维度 | 物理位置 (SSOT) | 说明 |
|------|----------------|------|
| **服务定义** | `platform/24.openpanel/compose.yaml` | 资源与容器拓扑声明 |
| **部署任务** | `platform/24.openpanel/deploy.py` | Invoke 自动化部署流程入口 |
| **专属 ClickHouse** | `platform/24.openpanel/clickhouse/` + compose `op-ch` 服务 | 版本匹配的 ClickHouse 25.10 容器；配置与 init-db 脚本 |
| **运行时密钥** | **Vault** (`secret/platform/<env>/openpanel`) | 仅 `cookie_secret` 与 `resend_api_key`。Postgres 口令读自 `secret/platform/<env>/postgres` (`root_password`)，Redis 口令读自 `secret/platform/<env>/redis` (`password`)，专属 ClickHouse（`op-ch`）走无鉴权端口、不存凭据 |
| **外部路由** | Dokploy / Traefik ingress | 域名解析与 /api 路径 Stripping 配置 |

### Code as SSOT 索引

- **任务加载器**：参见 `tasks.py`
- **部署基类**：参见 `libs/deployer.py`

---

## 2. 架构模型

```mermaid
graph TD
    User["User Browser / SDK"] -->|HTTPS /api| Traefik["Traefik (Dokploy Ingress)"]
    User -->|HTTPS /| Traefik
    
    subgraph OpenPanel Application Stack
        Traefik -->|Route: /api/* (Strip Path)| API["op-api (Port 3000)"]
        Traefik -->|Route: /*| Dashboard["op-dashboard (Port 3000)"]
        Worker["op-worker (Queue Processor)"]
        OpCH["op-ch (Dedicated ClickHouse 25.10)"]
        API -->|event store| OpCH
        Worker -->|store| OpCH
    end

    subgraph Shared Platform Tier
        API -->|config/users| Postgres["Shared PostgreSQL (platform-postgres)"]
        API -->|queues/caching| Redis["Shared Redis DB 3 (platform-redis)"]
        Worker -->|queues| Redis
    end
```

### 关键决策 (Architecture Decision)

- **专属 ClickHouse（推翻"零冗余"原决策）**：原设计要求复用共享 `platform-clickhouse` 以零冗余。但该实例由 **SigNoz** 拥有并钉死在 **ClickHouse 25.5**（SigNoz v0.128 最新版仍发布 25.5.6），而 **OpenPanel v2 面向 ClickHouse 25.10**（依赖 `DateTime64` 做 TTL、以及 `query_plan_convert_any_join_to_semi_or_anti_join` 等新版查询设置），在 25.5 上迁移与运行均失败。两者版本要求**互斥且共享实例不可升级**（会破坏 SigNoz），因此 OpenPanel 运行**专属、版本匹配的 ClickHouse**（`op-ch`，25.10）。Postgres 与 Redis 仍复用共享实例。
- **Traefik 做 API Stripping**：Dashboard 与 API 同域；Traefik 将 `/api` 前缀剥离后转发到 `op-api` 端口 `3000`（与上游 Caddy `handle_path /api*` 一致），其余路径转发到 `op-dashboard:3000`。
- **原生迁移命令**：`op-api` 启动序列必须为「source 密钥 → 等待依赖 → `pnpm -r run migrate:deploy` → `pnpm start`」。跳过迁移会导致空表、健康检查永远 `ready:false`。
- **Vault-Init 密钥注入**：密钥由 Vault-Agent 渲染至 tmpfs `/vault/secrets/.env`（应用只读挂载于 `/secrets/.env`），各容器 Entrypoint 中 source 加载；非密钥项（URL、`ALLOW_*`、`CLICKHOUSE_URL` 等）经 compose `environment:` 注入。

---

## 3. 设计约束 (Dos & Don'ts)

### ✅ 推荐模式 (Whitelist)

- **Postgres 库分离**：必须在共享 `platform-postgres` 中为 OpenPanel 创建专属数据库 `openpanel`。
- **专属 ClickHouse**：`op-ch` 容器经 `clickhouse/init-db.sh` 自建 `openpanel` 库；版本须匹配 OpenPanel（当前 25.10）。
- **Redis 独立 DB**：使用 `platform-redis` 的 `DB 3` 进行隔离，避免缓存键值与 App 冲突。
- **Traefik 标签显式定义**：在 `compose.yaml` 中显式定义路由规则与 API Stripping 中间件，端口指向 `3000`。

### ⛔ 禁止模式 (Blacklist)

- **禁止让 OpenPanel 连接共享 `platform-clickhouse`**：版本不兼容（25.5 vs 25.10），且会与 SigNoz 争用。必须使用专属 `op-ch`。
- **禁止在 compose 中包含明文密码**：所有账号密码、API Token 必须流经 Vault 并在运行时渲染。
- **禁止跳过 op-api 迁移或省略 /api 路径剥离**：前者导致空 schema，后者导致 `op-api` 找不到 `/api/*` 路由返回 404。

---

## 4. 标准操作程序 (Playbooks)

### SOP-001: 首次部署与数据库初始化

- **触发条件**：新增 OpenPanel 服务
- **步骤**：
    1. 确保 Vault 中存在依赖密钥（`invoke openpanel.setup` 会自动校验并补齐）：
       - `secret/platform/<env>/openpanel`：`cookie_secret`（会话签名密钥，缺失时自动生成）、`resend_api_key`（可选，缺失时写入 `placeholder`）。
       - 依赖项复用既有路径：`secret/platform/<env>/postgres` 的 `root_password`、`secret/platform/<env>/redis` 的 `password`。
    2. 运行 `invoke openpanel.setup`：
       - 此脚本将在共享 `platform-postgres` 中创建 `openpanel` 数据库；专属 `op-ch` 容器启动时经 init-db 自建 `openpanel` 库。
       - 之后，脚本将在 Dokploy 中上线服务容器；`op-api` 启动时自动应用 Postgres + ClickHouse 迁移。
    3. 检查部署状态：`invoke openpanel.shared.status`。

> **首次登录**：临时将 `ALLOW_REGISTRATION` 设为 `true`（compose `environment:`）以创建第一个管理员账号，建号后改回 `false` 并重启 `op-api`，关闭公开注册。

### SOP-002: 数据库迁移与重置

- **触发条件**：系统升级或架构重组需要重置数据
- **步骤**：
    1. 连接到 `platform-postgres` 重置 `openpanel` 数据库。
    2. 连接到专属 `op-ch` 重置 `openpanel` 数据库（或删除 `op-ch-data` 卷）。
    3. 重跑容器，`op-api` 会在启动时自动重新应用迁移。

### SOP-003: 应用接入 — 按环境 client-id（Model B，Infra-014）

OpenPanel 是**单实例**；环境隔离靠**每个环境一个 project / client-id**（Model B），而非每环境一套实例。finance_report 在 `finance_report/finance_report/10.app/deploy.py` 的 `openpanel_clients` 映射中按 `ENV` 选取 client-id，并以运行时 env `OPENPANEL_CLIENT_ID` 注入（app + preview compose）：

| 环境 | OpenPanel project / client-id | 状态 |
|------|-------------------------------|------|
| `production` | `28bfa625-…f77550` | 已签发 |
| `staging` | `62d5cfe0-…af698f` | 已签发 |
| `preview`（所有 preview 别名共用一个 project） | `6e8f9d85-…f22dd` | 已签发（project `finance-preview` / client "preview web"，`#375`） |

> preview 各别名（`main` / `pr-<N>` / `commit-<sha7>`）共用同一个 `preview` project；别名维度的区分依赖事件属性 / `deployment.environment`，不再拆分 project。

### SOP-004: 查询分析数据（已发布 CLI）

OpenPanel 事件分析由 app 仓库已发布的查询 CLI `common/observability/openpanel_query.py` 完成，使用 OpenPanel query API token，存于 Vault `secret/platform/<env>/openpanel/api_key`。本仓库不重新实现查询逻辑。遥测（traces/logs）查询见 [ops.observability.md](ops.observability.md) 的 `invoke signoz.shared.query-logs` / `list-services`。

### SOP-005: 查询通道 — MCP（按需问 FE/BE 问题）

> 目标（EPIC-024 原始诉求）：**后期直接通过 OpenPanel / SigNoz 查前端/后端问题**。两边都有官方 MCP server，自托管均支持自定义 URL。**这是"能自动化的"查询通道**；funnel/看板的"创建"不能自动化（见 SOP-006）。

| MCP | 能力 | 自托管接入 |
|-----|------|-----------|
| **OpenPanel** (`api.openpanel.dev/mcp`，self-host `https://openpanel.${INTERNAL_DOMAIN}/mcp`) | **读分析 + `/track` 写事件**（`get_events` / `get_chart` 查询 + 事件上报；**不能**建 funnel/看板） | token = base64(`clientId:clientSecret`)；**需 `read` 或 `root` 类型 client**（现有 finance-* 是 `write` 型，用不了 MCP → 见 SOP-006 步骤 0 先建一个 read client），存 Vault `secret/platform/<env>/openpanel/mcp_token` |
| **SigNoz** (`SigNoz/signoz-mcp-server`，self-host) | **可读写**（`signoz_create_dashboard` / `create_alert` / 查询 traces·logs·metrics） | `SIGNOZ_URL=https://signoz.${INTERNAL_DOMAIN}` + `SIGNOZ-API-KEY`（复用 Vault `secret/platform/<env>/signoz` 的 api_key） |

MCP client 配置（占位 token 走 Vault，**勿提交明文**）：
```jsonc
{
  "mcpServers": {
    "openpanel": { "url": "https://openpanel.${INTERNAL_DOMAIN}/mcp?token=${OPENPANEL_MCP_TOKEN}" },
    "signoz":    { "url": "https://signoz.${INTERNAL_DOMAIN}/mcp", "headers": { "SIGNOZ-API-KEY": "${SIGNOZ_API_KEY}" } }
  }
}
```

### SOP-006: 基线 funnel + 事件板（OpenPanel 无写 API → 手动建）

> OpenPanel 的 funnel/看板**只能在 UI 里建**（REST 与 MCP 都没有创建 report/dashboard 的能力，仅 `/track`+读取）。声明式 intent 固化在 `finance_report/finance_report/observability/openpanel_analytics.json`（`invoke fr-observability.shared.print-openpanel-analytics` 校验+打印），本 SOP 是其**手动 apply**。事件名必须与 FE `ANALYTICS_EVENTS`（`apps/frontend/src/lib/analytics.ts`）+ BE `src/analytics.py` 一致。

0. **（前置，给 MCP 用）** OpenPanel → Settings → API Clients → 新建一个 **`read`** client → 复制其 MCP token → 存 Vault `secret/platform/<env>/openpanel/mcp_token`。
1. 登录 `https://openpanel.${INTERNAL_DOMAIN}`，选对应环境 project（`finance-production` / `finance-staging` / `finance-preview`）。
2. **建漏斗** "Upload → Report"：新建 Report → 图表类型 **Funnel** → 依次加 3 步事件 `upload_started` → `upload_succeeded` → `report_generated`，conversion window 设 **24h**，breakdown 选 `deployment.environment`，保存到一个 dashboard。
3. **建事件板** "Key product events"：同一 dashboard 加若干 Report，覆盖 `screen_view / signup / upload_started / upload_succeeded / upload_failed / report_generated / review_approved`，breakdown `deployment.environment`。
4. 验证：跑一遍真实 upload→report 流程，确认漏斗三步都有计数（事件由 FE 与 BE `report_generated` 同时上报，见 SOP-003）。

---

## 5. 验证与测试 (The Proof)

| 行为描述 | 验证方式 | 覆盖率 |
|----------|----------|--------|
| **接口健康探针** | `curl -fsSL https://openpanel.${INTERNAL_DOMAIN}/api/healthcheck` | ✅ Manual |
| **前端资源渲染** | `curl -fsSL -I https://openpanel.${INTERNAL_DOMAIN}` | ✅ Manual |
| **数据上报与落库** | 发送 Mock Event 后查询 ClickHouse 计数 | ✅ Manual / script |
