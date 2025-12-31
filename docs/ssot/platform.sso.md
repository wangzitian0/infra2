# 认证与授权 SSOT

> **SSOT Key**: `platform.sso`
> **核心定义**: 定义基于 Authentik (IdP) 的统一身份认证体系，以及应用接入规范。

---

## 1. 真理来源 (The Source)

> **原则**：身份由 Authentik 管理，应用通过 Forward Auth 或 OIDC 接入。

| 维度 | 物理位置 (SSOT) | 说明 |
|------|----------------|------|
| **Authentik 部署** | [`platform/10.authentik/compose.yaml`](../../platform/10.authentik/compose.yaml) | SSO 服务定义 |
| **部署任务** | [`platform/10.authentik/deploy.py`](../../platform/10.authentik/deploy.py) | pre-compose / composing / post-compose |
| **共享任务** | [`platform/10.authentik/shared_tasks.py`](../../platform/10.authentik/shared_tasks.py) | SSO 应用自动化 |
| **Token 初始化** | [`platform/10.authentik/init-token.sh`](../../platform/10.authentik/init-token.sh) | Root Token 创建 |

---

## 2. Token 层级

> **原则**：与 Vault Token 层级平行设计。

| Token | Vault Key | 权限范围 | 用途 |
|-------|-----------|---------|------|
| **AUTHENTIK_ROOT_TOKEN** | `secret/platform/production/authentik/root_token` | 全局管理 | 创建应用、管理组 |
| **AUTHENTIK_APP_TOKEN** | `secret/platform/production/<service>/sso_*` | 单应用 | 服务 SSO 配置（未来） |

```
Vault Token Hierarchy          Authentik Token Hierarchy
─────────────────────          ─────────────────────────
VAULT_ROOT_TOKEN               AUTHENTIK_ROOT_TOKEN
  ├─ vault-policy-X              ├─ create-proxy-app
  └─ VAULT_APP_TOKEN             └─ AUTHENTIK_APP_TOKEN
      (per-service)                  (per-service)
```

---

## 3. 访问控制模型

### 3.1 接入方式

| 方式 | 适用场景 | 说明 |
|------|---------|------|
| **Forward Auth** | 静态服务、无法改代码 | Traefik 中间件检查，通过标签配置 |
| **OIDC** | 原生支持 OIDC 的应用 | 标准 OAuth2 流程 |

### 3.2 组策略 (Group-Based Access)

访问由 **Expression Policy** 控制，检查组成员身份：

```python
# 策略表达式
return ak_is_group_member(request.user, name='admins')
```

| 组名 | 用途 | 创建方式 |
|------|-----|---------|
| `admins` | 平台管理员，可访问所有管理应用 | `invoke authentik.shared.setup-admin-group` |
| `developers` | 开发团队 | 手动创建 |
| `users` | 普通用户 | 手动创建 |

> **注意**：Authentik 没有固定的 admin 组名。组的 `is_superuser` 属性控制 Authentik 后台权限，与应用访问控制无关。

---

## 4. 标准操作程序 (Playbooks)

### SOP-001: 初始化 SSO 系统

```bash
# 1. 创建 Authentik Root Token（需要 Vault Root）
export VAULT_ROOT_TOKEN=<vault-admin-token>
invoke authentik.shared.create-root-token

# 2. 设置 admin 组并添加 akadmin
invoke authentik.shared.setup-admin-group
```

### SOP-002: 新增 Forward Auth 应用（推荐）

```bash
# 使用 CLI 自动创建
invoke authentik.shared.create-proxy-app \
  --name="Portal" \
  --slug="portal" \
  --external-host="https://home.zitian.party" \
  --internal-host="platform-portal" \
  --port=8080

# 多组访问
invoke authentik.shared.create-proxy-app \
  --name="Dev Tools" \
  --slug="devtools" \
  --external-host="https://dev.example.com" \
  --internal-host="devtools" \
  --allowed-groups="admins,developers"
```

**Compose 配置**（已自动生成，或手动添加）：

```yaml
labels:
  # Forward auth through Authentik
  - "traefik.http.middlewares.portal-auth.forwardauth.address=http://platform-authentik-server:9000/outpost.goauthentik.io/auth/traefik"
  - "traefik.http.middlewares.portal-auth.forwardauth.trustForwardHeader=true"
  - "traefik.http.middlewares.portal-auth.forwardauth.authResponseHeaders=X-authentik-username,X-authentik-groups,X-authentik-email,X-authentik-name,X-authentik-uid"
  - "traefik.http.routers.portal.middlewares=portal-auth@docker"
```

### SOP-003: 新增 OIDC 应用

- **触发条件**: 应用原生支持 OIDC
- **步骤**:
    1. 登录 Authentik UI: `https://sso.${INTERNAL_DOMAIN}`
    2. 创建 **Provider (OAuth2/OIDC)**，记录 Client ID/Secret
    3. 创建 **Application** 并绑定 Provider
    4. 将 Client Secret 写入 Vault：
       ```bash
       vault kv put secret/platform/production/<app> \
         client_id=... \
         client_secret=...
       ```
    5. 在应用侧配置 OIDC 回调

### SOP-004: 轮换 Client Secret

1. 在 Authentik UI 重新生成 Secret
2. 更新 Vault 中对应路径
3. 触发应用重启加载新配置

---

## 5. 访问流程

```
用户访问 https://home.zitian.party
         │
         ▼
┌────────────────────┐
│      Traefik       │
│  Forward Auth 检查 │
└────────────────────┘
         │
         ▼
┌────────────────────┐     未登录      ┌────────────────────┐
│  Authentik Server  │ ───────────────▶│   登录页面         │
│  检查 session      │                 │   输入用户名密码   │
└────────────────────┘                 └────────────────────┘
         │ 已登录                              │
         ▼                                     │
┌────────────────────┐                         │
│  检查组成员身份    │◀────────────────────────┘
│  ak_is_group_member│
└────────────────────┘
         │
    ┌────┴────┐
    │         │
  在组       不在组
    │         │
    ▼         ▼
  200 OK    403 Forbidden
  转发请求   拒绝访问
```

---

## 6. 设计约束 (Dos & Don'ts)

### ✅ 推荐模式 (Whitelist)

- **Forward Auth 优先**：静态服务使用 Forward Auth，无需改代码
- **组策略管理**：通过 Authentik Group 管理访问权限
- **CLI 自动化**：使用 `invoke authentik.shared.*` 任务

### ⛔ 禁止模式 (Blacklist)

- **反模式 A**: 禁止多层认证叠加（Portal Gate + App OIDC）
- **反模式 B**: 禁止共享 Client Secret（每应用独立）
- **反模式 C**: 禁止在代码中硬编码 Token

---

## 7. 验证与测试 (The Proof)

```bash
# 1. 检查 Authentik 健康状态
invoke authentik.shared.status

# 2. 列出已配置的应用
invoke authentik.shared.list-apps

# 3. 测试未登录访问（应重定向）
curl -I https://home.zitian.party
# Expected: 302 → sso.zitian.party

# 4. 测试已登录访问
# 浏览器登录后，检查 Network 面板
# Expected: 200 OK，响应包含 X-authentik-* headers

# 5. 测试非管理员访问
# 创建普通用户，不加入 admins 组
# Expected: 403 Forbidden
```

---

## 8. 故障排查

| 问题 | 可能原因 | 解决方案 |
|------|---------|---------|
| "Token not found in Vault" | Root Token 未创建 | `invoke authentik.shared.create-root-token` |
| 401 Unauthorized on API | Token 过期或无效 | 重新创建 Root Token |
| 403 Forbidden after login | 用户不在允许的组 | 将用户加入组或检查策略配置 |
| Forward Auth 不生效 | Traefik 配置错误 | 检查 compose labels 和 Traefik logs |
| 重定向循环 | 域名配置不匹配 | 检查 external_host 与实际域名 |

---

## Used by

- [docs/ssot/README.md](./README.md)
- [docs/onboarding/05.sso.md](../onboarding/05.sso.md)
- [platform/10.authentik/README.md](../../platform/10.authentik/README.md)
- [platform/21.portal/README.md](../../platform/21.portal/README.md)
