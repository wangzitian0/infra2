# OpenPanel SSOT

> **SSOT Key**: `platform.openpanel`
> **核心定义**：OpenPanel 是一款开源产品分析平台。其在 Dokploy 中的部署将数据存储完全外包给共享的 PostgreSQL、Redis 和 ClickHouse 实例，实现零多余数据库容器的轻量化部署，并与 Vault 进行集成以确保密钥安全性。

---

## 1. 真理来源 (The Source)

| 维度 | 物理位置 (SSOT) | 说明 |
|------|----------------|------|
| **服务定义** | `platform/24.openpanel/compose.yaml` | 资源与容器拓扑声明 |
| **部署任务** | `platform/24.openpanel/deploy.py` | Invoke 自动化部署流程入口 |
| **运行时密钥** | **Vault** (`secret/platform/<env>/openpanel`) | 仅 `encryption_key` 与 `resend_api_key`。Postgres 口令读自 `secret/platform/<env>/postgres` (`root_password`)，Redis 口令读自 `secret/platform/<env>/redis` (`password`)，ClickHouse 走无鉴权 HTTP 端口、不存凭据 |
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
        Traefik -->|Route: /api/* (Strip Path)| API["op-api (Port 3333)"]
        Traefik -->|Route: /*| Dashboard["op-dashboard (Port 3000)"]
        Worker["op-worker (Queue Processor)"]
    end

    subgraph Shared Platform Tier
        API -->|config/users| Postgres["Shared PostgreSQL (platform-postgres)"]
        API -->|queues/caching| Redis["Shared Redis DB 3 (platform-redis)"]
        API -->|event store| ClickHouse["Shared ClickHouse (platform-clickhouse)"]
        
        Worker -->|queues| Redis
        Worker -->|store| ClickHouse
    end
```

### 关键决策 (Architecture Decision)

- **无本地数据库冗余**：不部署 OpenPanel 默认自带的 Redis/Postgres/ClickHouse 容器，使用全局共享存储，为 VPS 节省超过 2GB 内存并方便集中备份。
- **Traefik 做 API Stripping**：OpenPanel Dashboard 与 API 挂在同一个域名下，通过 Traefik 路由匹配。Traefik 将 `/api` 前缀去掉后，转发到 `op-api` 端口 `3333`。
- **Vault-Init 密钥注入**：密钥由 Vault-Agent 渲染至共享 tmpfs 卷 `/vault/secrets/.env`；应用容器以只读方式挂载于 `/secrets/.env`，并在 Entrypoint 中 source 加载该文件。

---

## 3. 设计约束 (Dos & Don'ts)

### ✅ 推荐模式 (Whitelist)

- **Postgres 用户与库分离**：必须为 OpenPanel 创建专属数据库 `openpanel`。
- **ClickHouse 库分离**：必须为 OpenPanel 创建专属数据库 `openpanel`。
- **Redis 独立 DB**：使用 `platform-redis` 的 `DB 3` 进行隔离，避免缓存键值与 App 冲突。
- **Traefik 标签显式定义**：在 `compose.yaml` 中显式定义 Traefik 路由规则与 API Stripping 中间件。

### ⛔ 禁止模式 (Blacklist)

- **严禁独立部署 ClickHouse/Postgres**：避免容器泛滥导致资源紧张。
- **禁止在 compose 中包含明文密码**：所有账号密码、API Token 必须流经 Vault 并在运行时渲染。
- **禁止省略 /api 路径剥离**：若未在 Traefik 中做路径剥离，`op-api` 会因为找不到 `/api/*` 路径路由而返回 404。

---

## 4. 标准操作程序 (Playbooks)

### SOP-001: 首次部署与数据库初始化

- **触发条件**：新增 OpenPanel 服务
- **步骤**：
    1. 确保 Vault 中存在依赖密钥（`invoke openpanel.setup` 会自动校验并补齐）：
       - `secret/platform/<env>/openpanel`：`encryption_key`（32 字节 hex，缺失时自动生成）、`resend_api_key`（可选，缺失时写入 `placeholder`）。
       - 依赖项复用既有路径：`secret/platform/<env>/postgres` 的 `root_password`、`secret/platform/<env>/redis` 的 `password`。
    2. 运行 `invoke openpanel.setup`：
       - 此脚本将自动在 `platform-postgres` 和 `platform-clickhouse` 中创建 `openpanel` 数据库。
       - 之后，脚本将在 Dokploy 中上线服务容器。
    3. 检查部署状态：`invoke openpanel.shared.status`。

### SOP-002: 数据库迁移与重置

- **触发条件**：系统升级或架构重组需要重置数据
- **步骤**：
    1. 连接到 `platform-postgres` 重置数据库。
    2. 连接到 `platform-clickhouse` 重置 `openpanel` 数据库。
    3. 重跑容器，API 容器会在启动时自动应用 prisma migrations。

---

## 5. 验证与测试 (The Proof)

| 行为描述 | 验证方式 | 覆盖率 |
|----------|----------|--------|
| **接口健康探针** | `curl -fsSL https://openpanel.${INTERNAL_DOMAIN}/api/healthcheck` | ✅ Manual |
| **前端资源渲染** | `curl -fsSL -I https://openpanel.${INTERNAL_DOMAIN}` | ✅ Manual |
| **数据上报与落库** | 发送 Mock Event 后查询 ClickHouse 计数 | ✅ Manual / script |
