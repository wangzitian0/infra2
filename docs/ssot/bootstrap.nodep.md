# Bootstrap 非 Terraform 管理组件 SSOT

> **SSOT Key**: `bootstrap.nodep`
> **核心定义**: Bootstrap 层中不通过 Terraform 管理的组件。

---

## 1. 组件清单

| 组件 | 安装方式 | 管理方式 | 状态 |
|------|----------|----------|------|
| **Dokploy** | 官方脚本 | Web UI | ✅ Active |
| **1Password Connect** | Docker Compose | Dokploy | ✅ Active |

---

## 2. Dokploy

### 安装

```bash
# 官方一键安装（在 VPS 上执行）
curl -sSL https://dokploy.com/install.sh | sh
```

### 访问

- **URL**: `https://dokploy.{INTERNAL_DOMAIN}`
- **默认端口**: 3000 (Traefik 代理)

### 管理

通过 Web UI 管理 Projects、Applications、Databases。

---

## 3. 1Password Connect

### 配置文件

[`bootstrap/self_host_1password.yaml`](https://github.com/wangzitian0/infra2/blob/main/bootstrap/self_host_1password.yaml)

### 服务

| 服务 | 端口 | 说明 |
|------|------|------|
| `op-connect-api` | 8080 | REST API（通过 Traefik 暴露） |
| `op-connect-sync` | 内部 | 同步服务 |

### 访问

- **URL**: `https://op.{INTERNAL_DOMAIN}`

---

## 4. 版本追踪

> 每次安装/升级后更新此表。

| 组件 | 当前版本 | 安装日期 | 备注 |
|------|----------|----------|------|
| Dokploy | latest | 2024-12 | 官方脚本安装 |
| 1Password Connect | latest | 2024-12 | Docker 镜像 |

---

## Used by

- [docs/ssot/README.md](./README.md)
