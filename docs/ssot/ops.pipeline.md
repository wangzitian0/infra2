# Pipeline SSOT (运维流水线)

> **SSOT Key**: `ops.pipeline`
> **核心定义**: 定义 CI/CD 流水线策略,包括文档站自动构建与 IaC Runner GitOps 自动化部署。

---

## 1. 真理来源 (The Source)

| 维度 | 物理位置 (SSOT) | 说明 |
|------|----------------|------|
| **Docs Workflow** | [`.github/workflows/docs-site.yml`](https://github.com/wangzitian0/infra2/blob/main/.github/workflows/docs-site.yml) | Pages 构建与发布 |
| **Infrastructure CI** | [`.github/workflows/infra-ci.yml`](https://github.com/wangzitian0/infra2/blob/main/.github/workflows/infra-ci.yml) | Compose 验证与部署器逻辑测试 |
| **IaC Runner** | [`bootstrap/06.iac-runner/`](https://github.com/wangzitian0/infra2/blob/main/bootstrap/06.iac-runner/) | GitOps 自动化部署 |
| **MkDocs 配置** | [`docs/mkdocs.yml`](../mkdocs.yml) | 站点结构与导航 |
| **依赖列表** | [`docs/requirements.txt`](../requirements.txt) | Python 依赖 |

---

## 2. IaC Runner (GitOps CD)

### 2.1 架构

```
┌─────────────┐     webhook      ┌──────────────┐     invoke sync   ┌─────────────┐
│   GitHub    │ ──────────────▶ │  IaC Runner  │ ─────────────────▶│  Services   │
│  (push)     │                  │  (container) │                   │  (Dokploy)  │
└─────────────┘                  └──────────────┘                   └─────────────┘
                                         │
                                         ▼
                                  GitHub Commit
                                  Status API
                                  (success/failure)
```

### 2.2 核心机制

| 机制 | 实现方式 | 文件 |
|------|----------|------|
| **Change Detection** | Path-based pattern matching | `webhook_server.py` |
| **Idempotency** | SHA256 hash (compose + env vars) → `IAC_CONFIG_HASH` | `libs/deployer.py` |
| **Service Discovery** | Auto-scan `*/deploy.py` files | `libs/deployer.discover_services()` |
| **Concurrency Safety** | File-based locking (`/workspace/.sync.lock`) | `sync_runner.py` |
| **GitHub Integration** | Commit Status API updates | `sync_runner.update_github_status()` |

### 2.3 Service Mapping

**自动发现规则**:
- `platform/{nn}.{service}/` → `{service}.sync`
- `finance_report/finance_report/{nn}.{service}/` → `fr-{service}.sync`
- `bootstrap/{nn}.{service}/` → `{service}.sync` (大多数排除)

**手动排除**:
- `bootstrap/vault` → None (高风险)
- `bootstrap/1password` → None
- `bootstrap/iac-runner` → None (避免自我重启)

### 2.4 触发条件

| Changed Path | Action |
|--------------|--------|
| `platform/{nn}.{service}/*` | Sync affected service |
| `finance_report/finance_report/{nn}.{service}/*` | Sync affected service |
| `libs/*` | Sync all discovered services |
| `bootstrap/*` | Skipped (manual only) |
| Other files | Ignored |

---

## 3. Docs Workflow

### 3.1 触发条件 (Triggers)

- **PR**: 任何 `docs/**` 或 `*.md` 变更将触发构建验证。
- **Push 到 main**: 自动构建并部署到 GitHub Pages。
- **手动**: 可在 GitHub Actions 手动触发。

### 3.2 构建与发布流程 (Build & Deploy)

1. 安装依赖：`pip install -r docs/requirements.txt`
2. 构建站点：`mkdocs build --config-file docs/mkdocs.yml`
3. 发布：GitHub Pages 使用 Actions 部署产物 `.site/`

---

## 4. Infrastructure CI

### 4.1 触发条件

- **PR/Push**: Changes to `bootstrap/`, `platform/`, `finance_report/`, `libs/`

### 4.2 验证流程

| Job | Purpose |
|-----|---------|
| **validate-compose** | Validate all `compose.yaml` syntax |
| **test-deployer-logic** | Test config hash idempotency & service discovery |
| **lint-python** | Ruff check & format validation |

---

## 5. 设计约束 (Dos & Don'ts)

### ✅ 推荐模式
- **合并前验证**: CI 通过是合并前提
- **幂等性优先**: Sync 基于 config hash,避免无效部署
- **自动服务发现**: 新增服务无需手动注册 (只需有 `deploy.py`)
- **GitHub 可见性**: Commit status 提供实时反馈

### ⛔ 禁止模式
- **禁止手动推送到 `gh-pages` 分支** (统一由 Actions 发布)
- **禁止跳过 CI** (即使紧急修复)
- **禁止直接修改 `SERVICE_TASK_MAP`** (使用自动发现)

---

## 6. 验证与测试 (The Proof)

| 行为描述 | 验证方式 | 状态 |
|----------|----------|------|
| **文档站构建成功** | `mkdocs build --config-file docs/mkdocs.yml` | ✅ Auto |
| **Pages 发布成功** | GitHub Actions `docs-site` workflow | ✅ Auto |
| **Compose 文件合法** | GitHub Actions `infra-ci` workflow | ✅ Auto |
| **Config Hash 幂等** | CI test in `infra-ci.yml` | ✅ Auto |
| **IaC Runner 健康** | E2E test `test_iac_runner.py` | ✅ Manual |
| **Webhook 签名验证** | E2E test `test_webhook_signature_validation` | ✅ Manual |

---

## 7. Secrets Management

| Secret | Vault Path | Purpose |
|--------|------------|---------|
| `WEBHOOK_SECRET` | `secret/bootstrap/production/iac-runner` | GitHub webhook HMAC 验证 |
| `GITHUB_TOKEN` | `secret/bootstrap/production/iac-runner` | GitHub Commit Status API (repo:status scope) |

---

## Used by

- [docs/ssot/README.md](./README.md)
- [bootstrap/06.iac-runner/README.md](../../bootstrap/06.iac-runner/README.md)
