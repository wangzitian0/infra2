# 文档索引

> **定位**：docs/ 目录索引与导航入口
> **文档首页**：`docs/onboarding/README.md`
> **在线站点**：https://wangzitian0.github.io/infra2/
> **说明**：仓库根目录 README 用于工程说明；本页是 docs 索引页

---

## 📌 快速入口

- **工程入口**：[README.md](../README.md)
- **文档首页**：[docs/onboarding/README.md](./onboarding/README.md)
- **SSOT 技术参考**：[docs/ssot/README.md](./ssot/README.md)
- **工程与文档准则**：[docs/ssot/core.engineering.md](./ssot/core.engineering.md)
- **合流门禁**：[docs/ssot/ops.merge-gate.md](./ssot/ops.merge-gate.md)
- **项目进度**：[docs/project/README.md](./project/README.md)
- **P0 值班 Runbook**：[Infra-022 五类故障](./runbooks/infra022-p0.md)
- **AI 行为准则**：[AGENTS.md](../AGENTS.md)
- **Workspace Harness**：[harness/README.md](../harness/README.md)

> 文档命令示例默认使用 `invoke`；未激活虚拟环境时请使用 `uv run invoke`。

---

## ✅ 推荐阅读顺序

1. [README.md](../README.md) - 工程全局入口
2. [docs/onboarding/README.md](./onboarding/README.md) - 场景式接入
3. [docs/ssot/README.md](./ssot/README.md) → [docs/ssot/core.md](./ssot/core.md) - 架构真理源
4. [docs/project/README.md](./project/README.md) - 当前项目与进度

---

## 🔧 基础设施维护者入口

- **Bootstrap**：[local](../bootstrap/README.md) · [GitHub](https://github.com/wangzitian0/infra2/blob/main/bootstrap/README.md)
- **Platform**：[local](../platform/README.md) · [GitHub](https://github.com/wangzitian0/infra2/blob/main/platform/README.md)
- **Libs**：[local](../common/README.md) · [GitHub](https://github.com/wangzitian0/infra2/blob/main/common/README.md)
- **Tools**：[local](../tools/README.md) · [GitHub](https://github.com/wangzitian0/infra2/blob/main/tools/README.md)
- **E2E Regression**：[local](../e2e_regressions/README.md) · [GitHub](https://github.com/wangzitian0/infra2/blob/main/e2e_regressions/README.md)

---

## 🛠️ 文档站构建

- `mkdocs build --config-file docs/mkdocs.yml`

---

*Last updated: 2026-07-18*
