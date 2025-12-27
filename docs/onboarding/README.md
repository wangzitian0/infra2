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
- **[使用数据库](./03.database.md)** - PostgreSQL、Redis、ClickHouse、ArangoDB
- **[管理密钥](./04.secrets.md)** - Vault 接入和凭据管理
- **[接入 SSO 登录](./05.sso.md)** - Casdoor OIDC 集成
- **[监控和分析](./06.observability.md)** - SigNoz 追踪 + OpenPanel 分析

---

## 📖 知识地图 (SSOT)

本指南侧重于“如何做”。关于“为什么”以及具体的架构、规则和 SOP，请务必查阅：

👉 **[SSOT (Single Source of Truth) 技术参考手册](../ssot/README.md)**

| 查阅内容 | 对应 SSOT 文件 |
|----------|---------------|
| **认证逻辑/SSO** | [`platform.auth.md`](../ssot/platform.auth.md) |
| **密钥管理流程** | [`platform.secrets.md`](../ssot/platform.secrets.md) |
| **数据库接入规范** | [`db.overview.md`](../ssot/db.overview.md) |
| **流水线命令** | [`ops.pipeline.md`](../ssot/ops.pipeline.md) |
| **故障恢复** | [`ops.recovery.md`](../ssot/ops.recovery.md) |

---

## 🆘 故障排查

遇到问题时，请遵循以下路径：

1. **查看教程末尾的 FAQ**。
2. **查阅 [故障恢复 SSOT](../ssot/ops.recovery.md)** 了解系统级状态。
3. **联系运维团队**。

---

*Last updated: 2025-12-25*