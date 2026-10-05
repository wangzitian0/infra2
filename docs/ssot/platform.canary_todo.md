# Canary Todo 平台基建校验工具 SSOT

> **SSOT Key**: `platform.canary_todo`
> **核心定义**：Canary Todo (`platform/30.todo`) 是部署于 `todo.zitian.party` 的合成探针与平台基建验收服务。它通过网络协议与端点握手，自证与验收平台基础设施（Postgres, Redis, S3/MinIO, SigNoz, OpenPanel, Authentik SSO）的运行时可达性。深层带鉴权读写与追踪遥测闭环跟踪于 Issue #973。

---

## 1. 真理来源 (The Source)

| 维度 | 物理位置 (SSOT) | 说明 |
|------|----------------|------|
| **服务代码与探针** | `platform/30.todo/app.py` | 具备物理探针与能力校验矩阵的轻量服务 |
| **容器拓扑** | `platform/30.todo/compose.yaml` | 资源限制、网络拓扑与 Traefik 路由 |
| **构建镜像** | `platform/30.todo/Dockerfile` | 钉牢 `infra2-sdk` 版本的 Python 3.11 镜像 |
| **部署入口** | `platform/30.todo/deploy.py` | `TodoDeployer` 与 Authentik SSO 自动化注册任务 |
| **外部路由** | Traefik / Dokploy Ingress | 公网绕过路由与 Authentik ForwardAuth 保护路由 |

### Code as SSOT 索引

- **服务部署器**：`TodoDeployer` (`platform/30.todo/deploy.py`)
- **健康检查探针**：`GET /api/health`（公网绕过）
- **基建验收状态**：`GET /api/canary/status`（公网绕过）
- **SSO 受保护看板**：`GET /`（经由 Authentik ForwardAuth 保护）

---

## 2. 架构模型与验证链路

```mermaid
graph TD
    User["外部探测器 / 开发者"] -->|HTTPS /api/health 或 /api/canary/status| Traefik["Traefik (Dokploy Ingress)"]
    User -->|HTTPS / (受控访问)| Traefik
    
    subgraph Ingress Routing
        Traefik -->|优先级 100: 无鉴权放行| Bypass["/api/health & /api/canary/status"]
        Traefik -->|优先级 10: Authentik ForwardAuth| AuthCheck["Authentik Outpost"]
        AuthCheck -->|认证通过| ProtectedUI["/ (Todo Web Dashboard)"]
    end

    subgraph Canary Todo Service
        Bypass --> TodoApp["platform-todo:8000 (app.py)"]
        ProtectedUI --> TodoApp
    end

    subgraph Platform Physical Reality Probes
        TodoApp -->|StartupMessage Handshake| PG["platform-postgres:5432"]
        TodoApp -->|PING & SETEX/GET Lifecycle| Redis["platform-redis:6379"]
        TodoApp -->|S3 Health & Presigned URL| S3["platform-s3:9000"]
        TodoApp -->|OTel Collector Health Check| SigNoz["platform-signoz-otel-collector:13133"]
        TodoApp -->|Analytics Ingest API| OpenPanel["platform-openpanel-api:3000"]
        TodoApp -->|SSO Endpoint Liveness| Authentik["platform-authentik-server:9000"]
    end
```

### 关键决策 (Architecture Decision)

1. **双轨路由分流 (Bypass vs SSO-Protected)**：
   - 合成探针和健康检查（`/api/health`, `/api/canary/status`）以 Traefik 优先级 100 直接放行，不走 SSO 重定向，供 CI 及外部监控检测。
   - 交互式看板（`/`）以优先级 10 绑定 Authentik ForwardAuth 中间件，供团队成员以单点登录身份安全访问。
2. **物理真源可达性探测 (Physical Reality Reachability)**：
   - 探针直接对 Postgres 发送 StartupMessage 握手、向 Redis 校验 PING 及 SETEX/GET 读写周期、请求 S3 HEAD 与 SigNoz 健康端点，并在 `/api/canary/status` 暴露诊断状态。真实带鉴权 CRUD 读写与 OTel trace 追踪上报在 Issue #973 中分阶段实现。
3. **SDK 一致性保证**：
   - `Dockerfile` 中的 `infra2-sdk` 版本由 `libs/tests/test_sdk_contract_adoption.py` 自动化测试严格守护，与 `pyproject.toml` 保持 100% 对齐。

---

## 3. 设计约束 (Dos & Don'ts)

### ✅ 推荐模式
- 平台新组件接入时，在 `app.py` 中扩充能力探测项，实现基建即代码的闭环自证。
- 部署后使用 `invoke todo.sso-setup` 自动化注册 Authentik 代理应用。

### ⛔ 禁止模式
- 禁止破坏公网放行路径 `/api/health` 与 `/api/canary/status` 的高优先级路由（会导致探测死锁）。
- 禁止在 `Dockerfile` 中手写与 `pyproject.toml` 漂移的 SDK wheel 地址。
