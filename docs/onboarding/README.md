# Infra 文档首页

> **定位**：Infra 文档站首页（入口 + 导航 + 场景）
> **在线站点**：https://wangzitian0.github.io/infra2/
> **源码入口**：本仓库 `docs/` 目录

---

> 命令示例默认使用 `invoke`；未激活虚拟环境时请使用 `uv run invoke`。

## 你是谁？

- **应用开发者**：从 5 分钟快速开始入门，按场景接入数据库/密钥/SSO。
- **基础设施维护者**：先看 SSOT Core + Ops Standards，再进入 Bootstrap / Platform 目录操作。
- **文档/项目协作**：查看 Project Portfolio，按模板记录变更和 TODO。

---

## 🚀 开发者接入路径 (Onboarding)

按顺序完成以下场景：

1. **[5 分钟快速开始](./01.quick-start.md)** - 了解平台能力与基本流程
2. **[部署第一个应用](./02.first-app.md)** - 端到端发布流程
3. **[使用数据库](./03.database.md)** - PostgreSQL/Redis 接入
4. **[管理密钥](./04.secrets.md)** - Vault 获取与管理
5. **[接入 SSO](./05.sso.md)** - Authentik OIDC
6. **[监控和分析](./06.observability.md)** - 规划中

---

## 📖 SSOT 技术真理源

需要理解架构与规范时，优先查阅 SSOT：

- **[SSOT Index](../ssot/README.md)** - 话题入口与索引
- **[核心架构](../ssot/core.md)** - 分层与依赖
- **[变量与密钥](../ssot/bootstrap.vars_and_secrets.md)** - 变量契约与密钥流转
- **[运维规范](../ssot/ops.standards.md)** - 防御性规则与约束
- **[恢复 SOP](../ssot/ops.recovery.md)** - 故障恢复路径

---

## 🧰 基础设施维护入口

- **Bootstrap (L1)**：https://github.com/wangzitian0/infra2/blob/main/bootstrap/README.md
- **Platform (L2)**：https://github.com/wangzitian0/infra2/blob/main/platform/README.md
- **Tools (env_tool)**：https://github.com/wangzitian0/infra2/blob/main/tools/README.md

---

## 🧭 项目治理

- **[Project Portfolio](../project/README.md)** - 全量项目索引
- **进行中项目**：`docs/project/Infra-XXX.*.md`
- **归档项目**：`docs/project/archive/`

---

## ✍️ 文档维护

- **构建文档站**：`mkdocs build --config-file docs/mkdocs.yml`
- **Project 模板**：`docs/project/project_template.md`
- **TODOWRITE 模板**：`docs/project/todowrite_template.md`
- **SSOT 模板**：`docs/ssot/template.md`

---
*Last updated: 2025-12-30*
