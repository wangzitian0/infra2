# Bootstrap

> **定位**：引导层，包含系统启动所需的基础组件安装  
> **SSOT 参考**：[docs/ssot/bootstrap.nodep.md](../docs/ssot/bootstrap.nodep.md)

---

## 📁 目录结构

```
./
├── 01.dokploy_install/    # Dokploy 安装
├── 02.dns_and_cert/       # DNS 和证书配置
├── 03.dokploy_setup/      # Dokploy 域名和 CLI 配置
├── 04.1password/          # 1Password Connect
├── 05.vault/              # HashiCorp Vault
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

###  1. [Dokploy 安装](./01.dokploy_install/)
VPS 初始化时安装 Dokploy 容器平台。

**状态**：✅ 已部署  
**域名**：`cloud.$INTERNAL_DOMAIN`

### 2. [DNS 和证书](./02.dns_and_cert/)
配置 Cloudflare DNS 和 Traefik HTTPS 证书。

**状态**：✅ 已配置  
**手动配置域名**：`cloud`, `op`, `sso`, `digger`

### 3. [Dokploy 配置](./03.dokploy_setup/)
配置 Dokploy 域名访问和 CLI 工具。

**状态**：✅ 已配置  
**依赖**：DNS 配置完成

### 4. [1Password Connect](./04.1password/)
自托管密钥管理服务。

**状态**：✅ 已部署  
**域名**：`op.$INTERNAL_DOMAIN`  
**API 版本**：1.8.1

### 5. [Vault](./05.vault/)
HashiCorp Vault 秘密管理。

**状态**：⏭️ 待部署  
**域名**：`vault.$INTERNAL_DOMAIN`

---

## 🔗 相关文档

- [SSOT: Bootstrap 组件](../docs/ssot/bootstrap.nodep.md)
- [SSOT: 核心架构](../docs/ssot/core.md)
- [总览: 文档索引](../docs/ssot/README.md)
