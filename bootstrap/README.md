# Bootstrap

> **定位**：引导层，包含系统启动所需的基础组件安装  
> **SSOT 参考**：[docs/ssot/bootstrap.nodep.md](../docs/ssot/bootstrap.nodep.md)

---

## 📁 目录结构

```
./
├── 01.dokploy_install/    # Dokploy 安装 + 主机防火墙（hostfw/）
├── 02.dns_and_cert/       # DNS 和证书配置
├── 03.dokploy_setup/      # Dokploy 域名和 CLI 配置
├── 04.1password/          # 1Password Connect
├── 05.vault/              # HashiCorp Vault
├── 06.iac_runner/         # IaC Runner 部署控制面（/deploy）
└── README.md              # 本文件（组件索引）
```

---

## 🔧 如何修改本目录

### 修改前必读

1. **阅读 SSOT**：先查阅 [bootstrap.nodep.md](../docs/ssot/bootstrap.nodep.md) 了解设计约束
2. **确认影响范围**：Bootstrap 是 Trust Anchor，变更需谨慎
3. **检查依赖**：上层 Platform/Data 依赖本层

### 常见修改场景

| 场景 | 操作步骤 | 注意事项 |
|------|----------|------------|
| **添加新组件** | 1. 创建子目录 → 2. 编写 README → 3. 更新 SSOT | 记录版本信息 |
| **升级组件** | 1. 执行升级 → 2. 更新组件 README → 3. 更新 SSOT 版本表 | 备份数据 |
| **删除组件** | 1. 确认无依赖 → 2. 卸载 → 3. 更新文档 | ⚠️ 谨慎操作 |

---

## 📖 组件列表

###  1. [Dokploy 安装](./01.dokploy_install/README.md)
VPS 初始化时安装 Dokploy 容器平台。

**状态**：✅ 已部署  
**域名**：`cloud.$INTERNAL_DOMAIN`

### 2. [DNS 和证书](./02.dns_and_cert/README.md)
配置 Cloudflare DNS 和 Traefik HTTPS 证书。

**状态**：✅ 已配置  
**自动化域名**：`cloud`, `op`, `vault`, `sso`, `home`

### 3. [Dokploy 配置](./03.dokploy_setup/README.md)
配置 Dokploy 域名访问和 CLI 工具。

**状态**：✅ 已配置  
**依赖**：DNS 配置完成

### 4. [1Password Connect](./04.1password/README.md)
自托管密钥管理服务。

**状态**：✅ 已部署  
**域名**：`op.$INTERNAL_DOMAIN`  
**API 版本**：1.8.1

### 5. [Vault](./05.vault/README.md)
HashiCorp Vault 秘密管理。

**状态**：✅ 已部署  
**域名**：`vault.$INTERNAL_DOMAIN`

### 6. [IaC Runner](./06.iac_runner/README.md)
Platform 层服务的部署控制面。GitHub Actions 以 release tag 为坐标、经 `deploy_v2` 调用其签名 `/deploy`；
tag 推送自动晋升 staging，prod 由 `promote_prod=true` 显式放行。GitHub push webhook（`/webhook`）只验签、
**不部署**——合并不是发布（[ops.pipeline.md](../docs/ssot/ops.pipeline.md) §2）。

**状态**：✅ 已部署  
**域名**：`iac.$INTERNAL_DOMAIN`  
**管理范围**：Platform 层 `iac_pinned` 服务（fan-out 见 [deploy-dependencies.yaml](../docs/ssot/deploy-dependencies.yaml)）

---

## 🔗 相关文档

- [文档索引](../docs/README.md)
- [Project Portfolio](../docs/project/README.md)
- [AI 行为准则](../AGENTS.md)
- [SSOT: Bootstrap 组件](../docs/ssot/bootstrap.nodep.md)
- [SSOT: IaC Runner](../docs/ssot/bootstrap.iac_runner.md)
- [SSOT: 核心架构](../docs/ssot/core.md)
- [总览: 文档索引](../docs/ssot/README.md)
