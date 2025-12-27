# Bootstrap 非 Terraform 管理组件 SSOT

> **SSOT Key**: `bootstrap.nodep`
> **核心定义**: 定义 Bootstrap 层中由脚本或手动安装的组件，这些组件不通过 Terraform 管理。

---

## 1. 真理来源 (The Source)

> **原则**：此处记录的组件均为 Terraform 管控边界之外的基础设施，通过脚本或手动方式安装和管理。

| 组件 | 安装方式 | 版本/来源 | 说明 |
|------|----------|-----------|------|
| **Dokploy** | curl 脚本 | [dokploy/dokploy](https://github.com/dokploy/dokploy) | PaaS 平台（Vercel/Heroku 替代） |

---

## 2. Dokploy

> **定位**：自托管 PaaS 平台，用于部署应用和管理 Docker 容器。
> **官方文档**：[docs.dokploy.com](https://docs.dokploy.com)

### 2.1 功能特性

- **应用部署**：支持 Node.js、PHP、Python、Go、Ruby 等各类应用
- **数据库管理**：MySQL、PostgreSQL、MongoDB、MariaDB、Redis
- **自动备份**：支持数据库备份到外部存储
- **Docker Compose**：原生支持复杂应用编排
- **Traefik 集成**：自动路由和负载均衡
- **实时监控**：CPU、内存、存储、网络监控
- **通知集成**：Slack、Discord、Telegram、Email 等

### 2.2 安装步骤

**前置条件**：
- VPS 已就绪且可 SSH 访问
- 目标机器有足够的资源（建议 2GB+ RAM）

**安装命令**：

```bash
curl -sSL https://dokploy.com/install.sh | sh
```

**验证安装**：

安装完成后，访问 `http://<VPS_IP>:3000` 进入 Dokploy 管理界面。

### 2.3 配置要点

| 配置项 | 值 | 说明 |
|--------|-----|------|
| 默认端口 | `3000` | Web UI 端口 |
| Docker Socket | `/var/run/docker.sock` | Docker 通信 |

---

## 3. 设计约束 (Dos & Don'ts)

### ✅ 推荐模式 (Whitelist)

- **模式 A**: 使用官方 curl 脚本安装，保持与上游同步。
- **模式 B**: 手动安装后记录版本信息到本文档。

### ⛔ 禁止模式 (Blacklist)

- **反模式 A**: **严禁** 尝试将 nodep 组件纳入 Terraform 管理（违背设计初衷）。
- **反模式 B**: **禁止** 不记录版本信息的"幽灵安装"。

---

## 4. 版本追踪

> **更新约定**：每次安装/升级后，更新此表。

| 组件 | 当前版本 | 安装日期 | 操作人 |
|------|----------|----------|--------|
| Dokploy | _待记录_ | _待记录_ | - |

---

## 5. 验证与测试 (The Proof)

| 行为描述 | 验证方式 | 状态 |
|----------|----------|------|
| **Dokploy 服务可达** | `curl -I http://<VPS_IP>:3000` | ⚠️ Pending |

---

## Used by

- [docs/ssot/README.md](./README.md)
- [docs/ssot/core.md](./core.md)