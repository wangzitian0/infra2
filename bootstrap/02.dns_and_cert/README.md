# DNS 和域名配置

配置 Cloudflare DNS 和 Traefik HTTPS 证书。

## 操作步骤

```bash
# 1. 登录 Cloudflare
# 访问 https://dash.cloudflare.com

# 2. 选择域名 $INTERNAL_DOMAIN

# 3. 添加 DNS 记录
# - 进入 DNS → Records
# - 手动添加以下 A 记录（Bootstrap 层服务）：

# | Name   | Type | Content    | Proxy status          |
# |--------|------|------------|-----------------------|
# | cloud  | A    | $VPS_HOST  | Proxied (橙色云朵) 🟠 |
# | op     | A    | $VPS_HOST  | Proxied (橙色云朵) 🟠 |
# | vault  | A    | $VPS_HOST  | Proxied (橙色云朵) 🟠 |
# | sso    | A    | $VPS_HOST  | Proxied (橙色云朵) 🟠 |

# 4. 验证 DNS 生效（可能需要 5-10 分钟）
dig cloud.$INTERNAL_DOMAIN
dig op.$INTERNAL_DOMAIN
dig vault.$INTERNAL_DOMAIN
dig sso.$INTERNAL_DOMAIN
```

## 域名说明

- `cloud.$INTERNAL_DOMAIN` → Dokploy Web UI
- `op.$INTERNAL_DOMAIN` → 1Password Connect API
- `vault.$INTERNAL_DOMAIN` → Vault UI/API
- `sso.$INTERNAL_DOMAIN` → Authentik SSO

## Cloudflare 代理模式（橙云）

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
> 当前域名均为手动配置，后续如引入自动化需更新 SSOT。
