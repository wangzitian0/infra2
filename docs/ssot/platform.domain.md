# 域名与路由架构 SSOT

> **SSOT Key**: `platform.domain`
> **核心定义**: 定义公网域名、容器域名、Traefik 路由的分层架构，以及 Dokploy 域名配置规范。

---

## 1. 真理来源 (The Source)

| 维度 | 物理位置 (SSOT) | 说明 |
|------|----------------|------|
| **公网 DNS** | Cloudflare Zone: `${INTERNAL_DOMAIN}` | 泛域名解析到 VPS_HOST |
| **DNS 自动化** | `bootstrap/02.dns_and_cert/tasks.py` | 管理 Cloudflare 记录 |
| **Traefik 配置** | 各服务 `compose.yaml` labels | 路由规则与中间件 |
| **Dokploy 配置** | 各服务 `deploy.py` | `subdomain` 参数控制 |
| **容器 DNS** | Docker 内部 DNS | 自动解析容器名 |

---

## 2. 域名分层架构

```
┌─────────────────────────────────────────────────────────────┐
│                      用户浏览器                              │
│                 https://home.zitian.party                    │
└──────────────────────┬──────────────────────────────────────┘
                       │ DNS 查询
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                    Cloudflare Edge                           │
│  - DNS: *.zitian.party → VPS_HOST                           │
│  - SSL: 边缘证书                                             │
└──────────────────────┬──────────────────────────────────────┘
                       │ HTTPS (443)
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                     Traefik (VPS)                            │
│  - 路由规则: compose.yaml labels                            │
│  - SSL: Let's Encrypt 源站证书                              │
│  - 中间件: ForwardAuth, CORS, etc.                          │
└──────────────────────┬──────────────────────────────────────┘
                       │
         ┌─────────────┼─────────────┐
         │             │             │
         ▼             ▼             ▼
┌────────────┐  ┌────────────┐  ┌────────────┐
│ Authentik  │  │   Portal   │  │   Vault    │
│ (SSO检查)  │  │  (服务)    │  │  (服务)    │
└────────────┘  └────────────┘  └────────────┘
     │               │               │
     └───────────────┴───────────────┘
              Docker 内部网络
         (容器名: platform-authentik-server${ENV_SUFFIX})
```

---

## 3. 域名类型与用途

### 3.1 公网域名 (Public Domain)

- **定义**: 由 `INTERNAL_DOMAIN` 环境变量定义的基础域名（如 `zitian.party`）
- **DNS**: Cloudflare 管理，通过 API 自动化
- **用途**: 用户访问入口、SSO 回调 URL
- **证书**: Cloudflare 边缘证书 + Traefik Let's Encrypt 源站证书

**规则**:
```bash
# Platform 服务（所有环境）
${SERVICE}${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}
# ENV_DOMAIN_SUFFIX: production -> ""，非生产 -> "-${ENV}"
# 示例: sso.zitian.party, home-staging.zitian.party
# 说明: ENV 内部用 '_'，域名中使用 '-'（如 staging_cn -> staging-cn），且不允许 '-' 或 '/'
```

### 3.2 容器域名 (Container Domain)

- **定义**: Docker 内部 DNS 自动解析的容器名
- **格式**: `${LAYER}-${SERVICE}${ENV_SUFFIX}` (如 `platform-authentik-server-staging`)
- **用途**: 容器间通信、Traefik ForwardAuth 地址
- **DNS**: Docker 内部 DNS (不可公网访问)

**规则**:
```yaml
# compose.yaml 中定义
services:
  authentik-server:
    container_name: platform-authentik-server${ENV_SUFFIX}
    # 其他容器可通过 platform-authentik-server${ENV_SUFFIX}:9000 访问
```

### 3.3 Dokploy 域名配置 (可选)

- **用途**: Dokploy UI 自动生成 Traefik labels
- **限制**: 与 compose.yaml labels 冲突
- **推荐**: SSO 保护的服务禁用 (`subdomain=None`)

---

## 4. 路由配置方式对比

| 方式 | 优势 | 劣势 | 适用场景 |
|------|------|------|---------|
| **compose.yaml labels** | 完全控制、支持复杂中间件、代码化 | 需手动编写 labels | SSO 保护、需要 ForwardAuth |
| **Dokploy UI 配置** | UI 友好、快速配置 | 与 labels 冲突、不支持复杂中间件 | 简单服务、无 SSO |
| **混合模式** | - | 配置分散、难以维护 | ⛔ 禁止 |

Simple public HTTP services such as IaC Runner and Wealthfolio should use
Dokploy Domains. Compose Traefik labels are reserved for routes that need
code-owned middleware or non-standard routing behavior.

**Verification**: `common/tests/test_domain_routing_policy.py` rejects deployers that set
`subdomain` while their compose file owns explicit `traefik.http.routers.*` or
`traefik.http.services.*` labels.

---

## 5. 标准操作程序 (Playbooks)

### SOP-001: 新增 SSO 保护服务

SSO 保护服务的完整配置清单与标准 Traefik ForwardAuth 标签定义见 [platform.sso.md §4 SOP-002](./platform.sso.md#sop-002-新增-forward-auth-应用推荐) 与 [§6 SSO 保护服务配置清单](./platform.sso.md#-sso-保护服务配置清单)：

1. `deploy.py` 设置 `subdomain = None`（禁用 Dokploy 域名配置，避免与手动 Traefik labels 冲突）。
2. `compose.yaml` 添加路由与 `${SERVICE}-auth${ENV_DOMAIN_SUFFIX}` ForwardAuth 中间件标签（含 `trustForwardHeader=true` 与 `authResponseHeaders`）。
3. 若子域名不在泛域名 `*.${INTERNAL_DOMAIN}` 覆盖范围内，运行 `DEPLOY_ENV=<env> invoke dns-and-cert.add --records=${SERVICE}${ENV_DOMAIN_SUFFIX}`。
4. 运行 `DEPLOY_ENV=<env> invoke authentik.shared.create-proxy-app` 创建 Authentik 代理应用。

### SOP-002: 新增非 SSO 服务

```bash
# 1. deploy.py 配置域名（可选）
subdomain = "${SERVICE}"  # 最终域名: ${SERVICE}${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}

# 2. compose.yaml 不需要 Traefik labels
# Dokploy 会自动生成基础路由

# 3. Cloudflare 添加 DNS 记录（若不在泛域名内）
DEPLOY_ENV=<env> invoke dns-and-cert.add --records=${SERVICE}${ENV_DOMAIN_SUFFIX}
```

### SOP-003: 排查域名冲突

```bash
# 1. 检查 Dokploy 配置
# 如果 Domain 字段有值，且 compose.yaml 有 labels，会冲突

# 2. 检查 Traefik 路由
docker logs bootstrap-traefik 2>&1 | grep "Router"

# 3. 检查 DNS 解析
dig ${SERVICE}${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}

# 4. 检查证书
curl -vI https://${SERVICE}${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}
```

---

## 6. 设计约束 (Dos & Don'ts)

### ✅ 推荐模式 (Whitelist)

- **SSO 服务用 labels**: `subdomain=None` + compose.yaml labels
- **简单服务用 Dokploy**: UI 配置域名，无需 labels
- **容器间通信用容器名**: `platform-authentik-server${ENV_SUFFIX}:9000`
- **公网访问用公网域名**: `https://sso.zitian.party`

### ⛔ 禁止模式 (Blacklist)

- **反模式 A**: 同时使用 Dokploy 域名配置和 compose.yaml labels（会冲突）
- **反模式 B**: 在 SSO 回调 URL 使用容器名（浏览器无法访问）
- **反模式 C**: 硬编码域名在代码中（应使用环境变量 `INTERNAL_DOMAIN`）
- **反模式 D**: 手动在 Cloudflare UI 添加记录后不同步到 tasks.py

---

## 7. 验证与测试 (The Proof)

```bash
# 1. 检查 DNS 解析
dig home.zitian.party
# Expected: A record → VPS_HOST

# 2. 检查 HTTPS 可达
curl -I https://home.zitian.party
# Expected: 200 OK 或 302 (SSO 重定向)

# 3. 检查 Traefik 路由
docker logs bootstrap-traefik 2>&1 | grep "home.zitian.party"
# Expected: Router created

# 4. 检查容器 DNS
docker exec platform-portal${ENV_SUFFIX} ping -c 1 platform-authentik-server${ENV_SUFFIX}
# Expected: 解析成功

# 5. 端到端测试（冒烟层，已接入 ops-checks.yml 的 e2e-regressions-smoke 夜间任务）
pytest e2e_regressions/tests/apps/test_portal_sso.py -m smoke
```

---

## 8. 故障排查

| 问题 | 可能原因 | 解决方案 |
|------|---------|---------|
| "502 Bad Gateway" | 容器未启动或端口错误 | 检查 `docker ps` 和 compose.yaml 端口配置 |
| "404 Not Found" | Traefik 路由未生效 | 检查 labels 语法和 Traefik logs |
| "SSL 证书无效" | Let's Encrypt 申请失败 | 检查 DNS 解析和 80 端口可达性 |
| "DNS 无法解析" | Cloudflare 记录缺失 | 运行 `invoke dns_and_cert.apply` |
| "重定向循环" | ForwardAuth 地址错误 | 检查 middleware 地址是否为容器名 |

---

## Used by

- [docs/ssot/core.md](./core.md)
- [docs/ssot/platform.sso.md](./platform.sso.md)
- [docs/ssot/bootstrap.dns_and_cert.md](./bootstrap.dns_and_cert.md)
