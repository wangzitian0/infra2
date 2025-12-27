# Documentation Center

> **定位**：文档体系总入口，连接三类文档
> **受众**：所有角色（开发者 + 运维者）

---

## 📚 文档体系说明

本平台的文档分为三类，各有侧重：

| 分类 | 路径 | 用途 | 适合人群 |
|------|------|------|---------|
| **[开发者体验](./onboarding/)** | `docs/onboarding/` | 场景驱动，注重接入顺滑 | 应用开发者 |
| **[SSOT](./ssot/)** | `docs/ssot/` | 关键信息集中，技术参考手册 | 所有人 |
| **Layer README** | 各目录 `README.md` | 模块驱动，设计和维护指南 | 基础设施维护者 |

---

## 🚀 开发者快速开始

**如果你是应用开发者**，从这里开始：

### [开发者接入指南](./onboarding/README.md)

场景驱动的完整指南：
1. **[5 分钟快速开始](./onboarding/01.quick-start.md)** - 了解平台能力
2. **[部署第一个应用](./onboarding/02.first-app.md)** - 端到端完整流程
3. **[使用数据库](./onboarding/03.database.md)** - PostgreSQL/Redis/ClickHouse
4. **[管理密钥](./onboarding/04.secrets.md)** - Vault 接入
5. **[接入 SSO](./onboarding/05.sso.md)** - Casdoor OIDC
6. **[监控和分析](./onboarding/06.observability.md)** - SigNoz + OpenPanel

---

## 📖 SSOT - 技术参考手册

**如果你需要查技术细节**，参考这里：

### [SSOT 话题索引](./ssot/README.md)

👉 **[Go to SSOT Documentation Index](./ssot/README.md)**

---

## 🔧 架构与设计文档

**如果你要修改基础设施**，参考这里：

### Layer 文档

- [Tools](../tools/README.md) - CI 工具和脚本
- [Bootstrap](../bootstrap/README.md) - 集群引导层 (L1)
- [Platform](../platform/README.md) - 平台服务层 (L2)
- [Data](../envs/README.md) - 数据层 (L3)
- [Apps](../apps/README.md) - 业务应用层 (L4)

### 项目治理

- [Project Status](./project/README.md) - 进行中的任务 (BRNs)
- [Change Log](./change_log/) - 变更历史
- [Deep Dives](./deep_dives/) - 深度技术决策文档
- [AGENTS.md](../AGENTS.md) - AI 行为准则

---
*Last updated: 2025-12-25*