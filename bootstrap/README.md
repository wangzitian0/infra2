# Bootstrap

> **定位**：引导层，包含系统启动所需的基础组件安装
> **SSOT 参考**：[docs/ssot/bootstrap.nodep.md](../docs/ssot/bootstrap.nodep.md)

---

## 📁 目录结构

```
./
└── README.md         # 本文件（操作手册）
```

---

## 🔧 如何修改本目录

### 修改前必读

1. **阅读 SSOT**：先查阅 [bootstrap.nodep.md](../docs/ssot/bootstrap.nodep.md) 了解设计约束
2. **确认影响范围**：Bootstrap 是 Trust Anchor，变更需谨慎
3. **检查依赖**：上层 Platform/Data 依赖本层

### 常见修改场景

| 场景 | 操作步骤 | 注意事项 |
|------|----------|----------|
| **添加新组件** | 1. 安装 → 2. 更新本 README → 3. 更新 SSOT | 记录版本信息 |
| **升级组件** | 1. 执行升级 → 2. 更新 SSOT 版本表 | 备份数据 |
| **删除组件** | 1. 确认无依赖 → 2. 卸载 → 3. 更新 README 和 SSOT | ⚠️ 谨慎操作 |

---

## 📖 操作指南

### Dokploy 安装

**触发条件**：新 VPS 初始化

```bash
# 1. SSH 登录 VPS
ssh root@<VPS_IP>

# 2. 执行安装脚本
curl -sSL https://dokploy.com/install.sh | sh

# 3. 验证安装
docker ps | grep dokploy
curl -I http://localhost:3000
```

**后续步骤**：
- 访问 `http://<VPS_IP>:3000` 完成初始配置（创建账户）
- 更新 [SSOT 版本追踪表](../docs/ssot/bootstrap.nodep.md#4-版本追踪)

### DNS 和域名配置

**触发条件**：需要通过域名访问服务（Dokploy、1Password Connect 等）

```bash
# 1. 登录 Cloudflare
# 访问 https://dash.cloudflare.com

# 2. 选择域名 $INTERNAL_DOMAIN

# 3. 添加 DNS 记录
# - 进入 DNS → Records
# - 手动添加以下 A 记录（Bootstrap 层服务）：

# | Name   | Type | Content        | Proxy status          |
# |--------|------|----------------|-----------------------|
# | cloud  | A    | $VPS_HOST  | Proxied (橙色云朵) 🟠 |
# | op     | A    | $VPS_HOST  | Proxied (橙色云朵) 🟠 |
# | sso    | A    | $VPS_HOST  | Proxied (橙色云朵) 🟠 |
# | digger | A    | $VPS_HOST  | Proxied (橙色云朵) 🟠 |

# 4. 验证 DNS 生效（可能需要 5-10 分钟）
dig cloud.$INTERNAL_DOMAIN
dig op.$INTERNAL_DOMAIN
dig sso.$INTERNAL_DOMAIN
dig digger.$INTERNAL_DOMAIN
```

**域名说明**：
- `cloud.$INTERNAL_DOMAIN` → Dokploy Web UI
- `op.$INTERNAL_DOMAIN` → 1Password Connect API
- `sso.$INTERNAL_DOMAIN` → SSO 服务 (Platform 层)
- `home.$INTERNAL_DOMAIN` → Home Page
- `digger.$INTERNAL_DOMAIN` → Digger Orchestrator

**Cloudflare 代理模式（橙云）**：
- ✅ **直接启用** Proxied（橙色云朵）
- **优势**：
  - 隐藏真实 VPS IP
  - 免费 DDoS 防护
  - CDN 加速
  - Cloudflare Universal SSL（自动 HTTPS）
- **工作原理**：
  - Cloudflare 在边缘提供 HTTPS
  - Traefik 仍会申请 Let's Encrypt 证书（用于源站连接）
  - 两者可以共存

> [!NOTE]
> 将来其他域名会通过 Terraform 管理，这 4 个作为 Bootstrap 层手动配置

### Dokploy 域名配置

**触发条件**：配置 Dokploy 通过域名访问（HTTPS）

**前提条件**：
- ✅ DNS 已生效（`dig cloud.${INTERNAL_DOMAIN}` 返回正确 IP）

```bash
# 1. 访问 Dokploy（通过 IP:3000）
# 浏览器打开 http://<VPS_IP>:3000

# 2. 登录后进入 Settings → Server

# 3. 配置 Server Domain
# - Host: cloud.${INTERNAL_DOMAIN}
# - Port: 3000
# - Enable SSL: Yes
# - Certificate: Let's Encrypt

# 4. 保存并等待证书申请（1-2 分钟）

# 5. 验证 HTTPS 访问
curl -I https://cloud.${INTERNAL_DOMAIN}
```

**说明**：
- Dokploy 会自动配置 Traefik 路由规则
- Let's Encrypt 自动申请 HTTPS 证书（用于源站）
- Cloudflare 提供边缘 HTTPS（Universal SSL）
- 证书申请成功后自动重定向到 HTTPS

### Dokploy CLI 配置

**触发条件**：本地开发机接入 Dokploy

**前提条件**：
- ✅ Dokploy 域名已配置（可通过 `https://cloud.${INTERNAL_DOMAIN}` 访问）

```bash
# 1. 安装 CLI
npm install -g @dokploy/cli

# 2. 认证配置
dokploy authenticate
# 输入服务器 URL: https://cloud.${INTERNAL_DOMAIN}
# 输入 Token: 从 Dokploy Web UI → Settings → API 获取

# 3. 验证认证
dokploy verify
```

**常用命令**：

```bash
dokploy project    # 项目管理
dokploy app        # 应用管理
dokploy database   # 数据库管理
dokploy env        # 环境变量管理
```


### 1Password Connect 安装

**触发条件**：需要自托管密钥管理服务

```bash
# 1. 在 1Password Web 获取 credentials
# 前往 https://my.1password.com/integrations/
# 创建 1Password Connect Server，下载 1password-credentials.json

# 2. 准备数据目录（SSH 到 VPS）
ssh root@<VPS_IP>
mkdir -p /data/1password

# 3. 登录 Dokploy 创建应用
# 访问 https://cloud.${INTERNAL_DOMAIN}
# - 创建 Project: bootstrap
# - 创建 Docker Compose 应用: 1password-connect
# - Repository: GitHub → wangzitian0/infra2 (推荐)
#   - Branch: main
#   - Compose Path: bootstrap/self_host_1password.yaml
# - 或手动粘贴 Compose 内容
# - Files: 上传 1password-credentials.json

# 4. 部署并验证
curl https://op.${INTERNAL_DOMAIN}/health
```

**前提条件**：
- ✅ 已完成 [DNS 和域名配置](#dns-和域名配置)
- ✅ `https://cloud.${INTERNAL_DOMAIN}` 和 `https://op.${INTERNAL_DOMAIN}` 可访问

**域名访问**：
- `https://op.${INTERNAL_DOMAIN}` - 1Password Connect API
- ⚠️ **安全性**：端口未暴露，**无法**通过 `http://IP:8080` 直接访问

**后续步骤**：
- 记录 Connect Token 到安全位置
- 更新 [SSOT 版本追踪表](../docs/ssot/bootstrap.nodep.md#4-版本追踪)

---

## 相关文档

- **SSOT**：[bootstrap.nodep.md](../docs/ssot/bootstrap.nodep.md) - 非 TF 组件定义
