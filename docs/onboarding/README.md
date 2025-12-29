# 开发者接入指南

> **面向角色**：应用开发者
> **目标**：快速、顺滑地将应用接入基础设施平台

---

## 🚀 场景驱动教程

根据你的需求，选择对应的场景指南：

### 新手入门
- **[5 分钟快速开始](./01.quick-start.md)** - 了解平台能力和核心概念
- **[部署第一个应用](./02.first-app.md)** - 端到端完整流程

### 核心能力接入
- **[使用数据库](./03.database.md)** - PostgreSQL、Redis（其余规划中）
- **[管理密钥](./04.secrets.md)** - Vault 接入和凭据管理
- **[接入 SSO 登录](./05.sso.md)** - Authentik OIDC 集成
- **[监控和分析](./06.observability.md)** - 规划中

---

## 📖 知识地图 (SSOT)

本指南侧重于“如何做”。关于“为什么”以及具体的架构、规则和 SOP，请务必查阅：

👉 **[SSOT (Single Source of Truth) 技术参考手册](../ssot/README.md)**

| 查阅内容 | 对应 SSOT 文件 |
|----------|---------------|
| **认证逻辑/SSO** | [`platform.sso.md`](../ssot/platform.sso.md) |
| **密钥管理流程** | [`bootstrap.vars_and_secrets.md`](../ssot/bootstrap.vars_and_secrets.md) |
| **数据库接入规范** | [`db.overview.md`](../ssot/db.overview.md) |
| **流水线命令** | [`ops.pipeline.md`](../ssot/ops.pipeline.md) |
| **故障恢复** | [`ops.recovery.md`](../ssot/ops.recovery.md) |

---

## ❓ 常见问题

**Q: 我不知道 INTERNAL_DOMAIN 是什么？**  
A: 由运维提供，或在有权限时通过 `invoke local.bootstrap` 读取 init/env_vars。

**Q: 我没有 Dokploy/Vault/SSO 权限怎么办？**  
A: 先完成应用构建与部署准备，权限相关操作由运维协助完成。

---

## 🆘 故障排查

遇到问题时，请遵循以下路径：

1. **查看上面的常见问题**。
2. **查阅 [故障恢复 SSOT](../ssot/ops.recovery.md)** 了解系统级状态。
3. **联系运维团队**。

---

*Last updated: 2025-12-25*
