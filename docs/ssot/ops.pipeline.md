# Pipeline SSOT (运维流水线)

> **SSOT Key**: `ops.pipeline`
> **核心定义**: 目前仅维护 **MkDocs 文档站** 的自动构建与发布；其他 CI/CD 流水线暂不在本仓库定义。

---

## 1. 真理来源 (The Source)

| 维度 | 物理位置 (SSOT) | 说明 |
|------|----------------|------|
| **Docs Workflow** | [`.github/workflows/docs-site.yml`](https://github.com/wangzitian0/infra2/blob/main/.github/workflows/docs-site.yml) | Pages 构建与发布 |
| **MkDocs 配置** | [`docs/mkdocs.yml`](../mkdocs.yml) | 站点结构与导航 |
| **依赖列表** | [`docs/requirements.txt`](../requirements.txt) | Python 依赖 |

---

## 2. 触发条件 (Triggers)

- **PR**: 任何 `docs/**` 或 `*.md` 变更将触发构建验证。
- **Push 到 main**: 自动构建并部署到 GitHub Pages。
- **手动**: 可在 GitHub Actions 手动触发。

---

## 3. 构建与发布流程 (Build & Deploy)

1. 安装依赖：`pip install -r docs/requirements.txt`
2. 构建站点：`mkdocs build --config-file docs/mkdocs.yml`
3. 发布：GitHub Pages 使用 Actions 部署产物 `.site/`

---

## 4. 设计约束 (Dos & Don'ts)

### ✅ 推荐模式
- MkDocs 输入源为 `docs/` 目录（`docs_dir: .`）。
- 变更导航请更新 `docs/mkdocs.yml`。

### ⛔ 禁止模式
- 禁止手动推送到 `gh-pages` 分支（统一由 Actions 发布）。

---

## 5. 验证与测试 (The Proof)

| 行为描述 | 验证方式 | 状态 |
|----------|----------|------|
| **文档站构建成功** | `mkdocs build --config-file docs/mkdocs.yml` | ✅ Manual |
| **Pages 发布成功** | GitHub Actions `docs-site` workflow | ✅ Manual |

---

## Used by

- [docs/ssot/README.md](./README.md)
