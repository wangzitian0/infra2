# Dokploy 配置

配置 Dokploy 域名访问和 CLI 工具。

## 域名配置

**前提条件**：DNS 已生效（建议先运行 `invoke dns_and_cert.setup`）

```bash
# 1. 访问 Dokploy（通过 IP:3000）
# 浏览器打开 http://<VPS_IP>:3000

# 2. 登录后进入 Settings → Server

# 3. 配置 Server Domain
# - Host: cloud.$INTERNAL_DOMAIN
# - Port: 3000
# - Enable SSL: Yes
# - Certificate: Let's Encrypt

# 4. 保存并等待证书申请（1-2 分钟）

# 5. 验证 HTTPS 访问
curl -I https://cloud.$INTERNAL_DOMAIN
```

## CLI 配置

**前提条件**：Dokploy 域名已配置

```bash
# 1. 安装 CLI
npm install -g @dokploy/cli

# 2. 认证配置
dokploy authenticate
# 输入服务器 URL: https://cloud.$INTERNAL_DOMAIN
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
