# DNS 和域名配置

配置 Cloudflare DNS 与证书相关设置（自动化）。

## 前置条件（0 帧起手）

- `init/env_vars` 已在 1Password 中创建（`VPS_HOST`, `INTERNAL_DOMAIN`）。
- 1Password 中存在 `bootstrap/cloudflare` item，包含：
  - `CF_API_TOKEN`
  - `CF_ZONE_ID`（可选，若缺省则通过 `CF_ZONE_NAME` 或 `INTERNAL_DOMAIN` 查找）
  - `CF_ZONE_NAME`（可选）

> `.env.example` 仅作 Key 清单，不存值。

## 自动化操作

默认管理的域名：

`cloud`, `op`, `vault`, `sso`, `home`

```bash
# 一键完成 DNS + SSL 设置 + HTTPS 预热
invoke dns_and_cert.setup

# 仅创建/更新 DNS 记录
invoke dns_and_cert.apply

# 仅更新 Cloudflare SSL 设置
invoke dns_and_cert.ssl

# 触发证书申请（HTTPS 预热）
invoke dns_and_cert.warm

# 验证 DNS/HTTPS
invoke dns_and_cert.verify
```

可选参数示例：

```bash
# 自定义子域名列表（逗号分隔）
invoke dns_and_cert.apply --records=cloud,op,vault,sso,home

# 关闭 Cloudflare 代理（灰云）
invoke dns_and_cert.apply --proxied=false
```

## 域名说明

- `cloud.$INTERNAL_DOMAIN` → Dokploy Web UI
- `op.$INTERNAL_DOMAIN` → 1Password Connect API
- `vault.$INTERNAL_DOMAIN` → Vault UI/API
- `sso.$INTERNAL_DOMAIN` → Authentik SSO
- `home.$INTERNAL_DOMAIN` → Homer Portal

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

## 手动兜底（仅紧急时使用）

如果自动化失败，可在 Cloudflare UI 手动创建 A 记录（指向 `VPS_HOST`）：

| Name   | Type | Content    | Proxy status          |
|--------|------|------------|-----------------------|
| cloud  | A    | $VPS_HOST  | Proxied (橙色云朵) 🟠 |
| op     | A    | $VPS_HOST  | Proxied (橙色云朵) 🟠 |
| vault  | A    | $VPS_HOST  | Proxied (橙色云朵) 🟠 |
| sso    | A    | $VPS_HOST  | Proxied (橙色云朵) 🟠 |
| home   | A    | $VPS_HOST  | Proxied (橙色云朵) 🟠 |
