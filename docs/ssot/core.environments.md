# 六环境架构 SSOT

> **SSOT Key**: `core.environments`  
> **核心定义**: 定义从本地开发到生产的六环境架构、各环境的用途、隔离策略、测试点和迭代速度。

---

## 1. 真理来源 (The Source)

| 维度 | 物理位置 (SSOT) | 说明 |
|------|----------------|------|
| **环境配置** | Dokploy Project/Environment | 远端环境变量和部署配置 |
| **环境选择** | `DEPLOY_ENV` 环境变量 | 部署时目标环境选择 |
| **数据隔离** | `DATA_PATH` / `ENV_SUFFIX` | 数据和容器名隔离 |
| **域名隔离** | `ENV_DOMAIN_SUFFIX` | 公网域名隔离 |

---

## 2. 架构模型 - 六环境设计

```mermaid
flowchart LR
    L1["Local Dev<br/>(开发机)"]
    L2["Local Docker<br/>(本地容器)"]
    T["Test<br/>(临时环境)"]
    S["Staging<br/>(集成测试)"]
    P["Production<br/>(生产环境)"]
    DR["DR<br/>(灾备)"]
    
    L1 -->|代码推送| L2
    L2 -->|PR提交| T
    T -->|测试通过| S
    S -->|验证通过| P
    P -.->|备份同步| DR
    
    style L1 fill:#e1f5fe
    style L2 fill:#e1f5fe
    style T fill:#fff9c4
    style S fill:#fff3e0
    style P fill:#c8e6c9
    style DR fill:#f3e5f5
```

---

## 3. 环境定义与特性

### 3.1 Local Development (本地开发)

**位置**: 开发者工作站  
**用途**: 代码编辑、单元测试、快速迭代  
**特点**:
- ✅ **零基础设施** - 无需 Docker/DB，纯代码开发
- ✅ **Mock 外部依赖** - 使用 pytest fixtures 模拟 DB/Redis/S3
- ✅ **热重载** - Next.js dev server + FastAPI --reload
- ✅ **IDE 集成** - LSP、调试器、类型检查

**配置**:
```bash
# Finance Report 示例
cd apps/backend
uv run pytest  # 单元测试（内存 SQLite）

cd apps/frontend
npm run dev  # Next.js dev server (localhost:3000)
```

**数据**: 无持久化，使用内存数据库或 Mock

**迭代速度**: ⚡⚡⚡⚡⚡ (秒级)

---

### 3.2 Local Docker (本地容器)

**位置**: 开发者工作站 (Docker Compose)  
**用途**: 端到端测试、集成测试、依赖验证  
**特点**:
- ✅ **完整栈** - PostgreSQL + Redis + MinIO + Backend + Frontend
- ✅ **真实依赖** - 真实数据库、真实 S3
- ✅ **快速重置** - `docker compose down -v && docker compose up`
- ✅ **调试友好** - 容器日志、端口映射

**配置**:
```bash
# Finance Report 示例
docker compose up -d postgres minio redis  # 启动依赖
moon run backend:dev  # 后端连接真实 DB
moon run frontend:dev  # 前端

# 或者完整栈
docker compose up  # 包括 backend + frontend
```

**数据**: `/tmp/finance_report/` 或 Docker volumes (可随时删除)

**迭代速度**: ⚡⚡⚡⚡ (分钟级)

---

### 3.3 Test (临时测试环境)

**位置**: VPS (Dokploy)  
**用途**: PR 预览、功能验证、临时测试、0-downtime bootstrap 验证  
**特点**:
- ✅ **临时部署** - PR 创建时自动部署，PR 合并后自动销毁
- ✅ **独立域名** - `report-pr-123.zitian.party`
- ✅ **独立数据** - `ENV_SUFFIX=-pr-123` 确保数据隔离
- ✅ **0帧起手** - 验证从零开始部署的完整流程
- ✅ **快速验证** - 无需等待 staging，直接测试新功能

**配置**:
```bash
# 通过 GitHub Actions 或手动触发
export DEPLOY_ENV=test
export ENV_SUFFIX=-pr-123
export DATA_PATH=/data/test/finance_report-pr-123

invoke fr-app.setup  # 自动创建 DB、bucket、部署应用
```

**数据**: `/data/test/finance_report-pr-{PR_NUMBER}/` (自动清理)

**域名**: `report-pr-{PR_NUMBER}.zitian.party`

**生命周期**:
- 创建: PR 提交时
- 销毁: PR 合并或关闭后 7 天

**测试点**:
- ✅ 功能正确性
- ✅ UI/UX 验证
- ✅ 基础性能测试
- ✅ 0-downtime bootstrap (新环境从零启动)

**迭代速度**: ⚡⚡⚡ (小时级)

---

### 3.4 Staging (集成测试环境)

**位置**: VPS (Dokploy)  
**用途**: 集成测试、回归测试、UAT、性能测试  
**特点**:
- ✅ **长期运行** - 永久环境，不销毁
- ✅ **生产镜像** - 与 production 相同的配置
- ✅ **完整测试** - E2E、性能、安全扫描
- ✅ **数据保留** - 测试数据持久化（用于回归测试）

**配置**:
```bash
export DEPLOY_ENV=staging
export ENV_SUFFIX=-staging  # 可选，用于容器名隔离
export DATA_PATH=/data/platform/staging  # 或使用 Dokploy Environment 配置

# Platform 服务
invoke postgres.setup  # platform-postgres-staging
invoke redis.setup     # platform-redis-staging
invoke minio.setup     # 域名: minio-staging.zitian.party

# Finance Report 应用
invoke fr-postgres.setup  # finance_report-postgres-staging
invoke fr-redis.setup
invoke fr-app.setup       # 域名: report-staging.zitian.party
```

**数据**: `/data/platform/staging/`, `/data/finance_report/staging/`

**域名**: `{service}-staging.zitian.party`

**测试点**:
- ✅ 完整 E2E 测试套件
- ✅ API 集成测试
- ✅ 性能基准测试
- ✅ 安全扫描 (OWASP ZAP)
- ✅ 数据库迁移测试
- ✅ 备份恢复测试

**迭代速度**: ⚡⚡ (天级)

---

### 3.5 Production (生产环境)

**位置**: VPS (Dokploy)  
**用途**: 生产服务、真实用户访问  
**特点**:
- ✅ **高可用** - 监控、告警、备份
- ✅ **性能优化** - 缓存、CDN、数据库优化
- ✅ **安全加固** - WAF、Rate limiting、Audit logs
- ✅ **数据备份** - 每日备份到 Cloudflare R2

**配置**:
```bash
export DEPLOY_ENV=production  # 默认值
# ENV_SUFFIX 为空 (production 不使用后缀)
# ENV_DOMAIN_SUFFIX 为空 (production 域名无后缀)

# Platform 服务
invoke postgres.setup  # platform-postgres
invoke redis.setup     # platform-redis
invoke minio.setup     # 域名: minio.zitian.party

# Finance Report 应用
invoke fr-postgres.setup  # finance_report-postgres
invoke fr-redis.setup
invoke fr-app.setup       # 域名: report.zitian.party
```

**数据**: `/data/platform/`, `/data/finance_report/`

**域名**: `{service}.zitian.party` (无后缀)

**部署策略**:
- ✅ 只部署经过 staging 完整测试的版本
- ✅ 使用 Git tags (如 `v1.2.3`)
- ✅ 数据库迁移先在 staging 验证
- ✅ 分批部署 (canary / blue-green)

**迭代速度**: ⚡ (周级)

---

### 3.6 DR (灾备环境)

**位置**: 备用 VPS (或 Cloudflare R2)  
**用途**: 数据备份、灾难恢复  
**特点**:
- ✅ **冷备份** - 数据每日同步，不运行服务
- ✅ **快速恢复** - 可在 1 小时内启动服务
- ✅ **成本优化** - 仅存储数据，不运行容器

**配置**:
```bash
# 备份策略 (每日执行)
# PostgreSQL → pg_dump → R2
# MinIO → rclone sync → R2
# Vault → backup snapshot → R2
```

**恢复 SLA**: < 1 小时 (RTO)  
**数据丢失**: < 24 小时 (RPO)

---

## 4. 环境隔离策略

### 4.1 容器名隔离

| 环境 | ENV_SUFFIX | 容器名示例 |
|------|-----------|-----------|
| Local Docker | (无) | `postgres`, `backend`, `frontend` |
| Test | `-pr-{PR}` | `finance_report-postgres-pr-123` |
| Staging | `-staging` | `platform-postgres-staging` |
| Production | (空) | `platform-postgres` |

### 4.2 域名隔离

| 环境 | ENV_DOMAIN_SUFFIX | 域名示例 |
|------|------------------|---------|
| Local | (localhost) | `localhost:3000` |
| Test | `-pr-{PR}` | `report-pr-123.zitian.party` |
| Staging | `-staging` | `report-staging.zitian.party` |
| Production | (空) | `report.zitian.party` |

### 4.3 数据隔离

| 环境 | DATA_PATH | 说明 |
|------|-----------|------|
| Local | `/tmp/` or volumes | 可随时删除 |
| Test | `/data/test/{app}-pr-{PR}/` | PR 关闭后自动清理 |
| Staging | `/data/{layer}/staging/` | 持久化 |
| Production | `/data/{layer}/` | 持久化 + 备份 |

### 4.4 Vault 密钥隔离

| 环境 | Vault 路径 | 示例 |
|------|----------|------|
| Test | `secret/data/{project}/test/{service}` | `secret/data/finance_report/test/app` |
| Staging | `secret/data/{project}/staging/{service}` | `secret/data/finance_report/staging/app` |
| Production | `secret/data/{project}/production/{service}` | `secret/data/finance_report/production/app` |

---

## 5. 测试门禁 (Quality Gates)

### 5.1 Local → PR (Test)

**门禁条件**:
- ✅ 单元测试通过 (>= 95% 覆盖率)
- ✅ Linter 无错误
- ✅ 类型检查通过
- ✅ 本地 E2E 测试通过

**自动化**: Pre-commit hooks + GitHub Actions

---

### 5.2 Test → Staging

**门禁条件**:
- ✅ Test 环境功能验证通过
- ✅ Code review 通过
- ✅ PR approved
- ✅ Conflicts 解决

**自动化**: GitHub Actions (merge to main)

---

### 5.3 Staging → Production

**门禁条件**:
- ✅ Staging E2E 测试全部通过
- ✅ 性能测试达标 (响应时间 < 200ms)
- ✅ 安全扫描无高危漏洞
- ✅ 数据库迁移在 staging 验证通过
- ✅ Changelog 更新
- ✅ 人工验收 (Product Owner approval)

**自动化**: GitHub Actions (tag release) + Manual approval

---

## 6. Test 环境特殊设计 - 0帧起手验证

### 6.1 目标

Test 环境的核心价值是验证**从零开始部署**的能力：
- ✅ 新 VPS 能否在 10 分钟内完成 Bootstrap
- ✅ 新项目能否在 5 分钟内部署完成
- ✅ 所有依赖能否自动创建（bucket、用户、密钥）

### 6.2 实现方式

**临时资源创建**:
```bash
# PR 创建时触发
export PR_NUMBER=123
export DEPLOY_ENV=test
export ENV_SUFFIX=-pr-${PR_NUMBER}

# 自动化脚本
invoke fr-app.setup  # 内部自动：
  1. 检测 Vault 中是否有密钥
  2. 如无 → 调用 minio.create-app-bucket 创建
  3. 如无 → 调用 postgres.create-user 创建
  4. 存储密钥到 Vault (test 环境)
  5. 部署应用
```

**自动清理**:
```bash
# PR 关闭后触发
invoke test.cleanup --pr-number=123
  1. 停止容器
  2. 删除数据目录
  3. 删除 Vault 密钥 (secret/data/finance_report/test/*)
  4. 删除 MinIO bucket
  5. 删除 Dokploy 域名配置
```

### 6.3 验证清单

每次 PR 部署到 Test 环境时，自动验证：
- [ ] MinIO bucket 自动创建
- [ ] PostgreSQL 用户自动创建
- [ ] Vault 密钥自动生成
- [ ] 应用健康检查通过
- [ ] 域名解析正确
- [ ] HTTPS 证书有效

---

## 7. 迭代速度对比

| 环境 | 部署频率 | 变更范围 | 自动化程度 | 回滚成本 |
|------|---------|---------|-----------|---------|
| Local Dev | 每次保存 | 单个文件 | 热重载 | 0 (无影响) |
| Local Docker | 每小时 | 多个服务 | docker compose up | 低 (重启容器) |
| Test | 每个 PR | 完整功能 | GitHub Actions | 低 (删除 PR 环境) |
| Staging | 每天 | 多个功能 | CI/CD | 中 (回滚版本) |
| Production | 每周 | 稳定版本 | Manual approval | 高 (影响用户) |
| DR | 被动触发 | 全量恢复 | 手动 | 极高 (业务中断) |

---

## 8. 配置示例

### 8.1 Finance Report Test 环境

```bash
# .github/workflows/pr-deploy.yml
name: Deploy PR to Test
on:
  pull_request:
    types: [opened, synchronize]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Deploy to Test
        env:
          DEPLOY_ENV: test
          ENV_SUFFIX: -pr-${{ github.event.pull_request.number }}
          PR_NUMBER: ${{ github.event.pull_request.number }}
        run: |
          cd repo/
          invoke fr-app.setup
```

### 8.2 Finance Report Staging 环境

```bash
# Dokploy Environment 配置 (staging)
DATA_PATH=/data/finance_report/staging
ENV_SUFFIX=-staging  # 可选
ENV_DOMAIN_SUFFIX=-staging

# Vault 密钥路径
secret/data/finance_report/staging/app/DATABASE_URL
secret/data/finance_report/staging/app/S3_BUCKET
```

### 8.3 Finance Report Production 环境

```bash
# Dokploy Environment 配置 (production)
DATA_PATH=/data/finance_report
# ENV_SUFFIX 留空
# ENV_DOMAIN_SUFFIX 留空

# Vault 密钥路径
secret/data/finance_report/production/app/DATABASE_URL
secret/data/finance_report/production/app/S3_BUCKET
```

---

## 9. 设计约束 (Dos & Don'ts)

### ✅ 推荐模式

1. **Test 环境用于快速验证**
   - PR 提交后立即部署到 Test
   - 功能验证后再合并到 Staging
   
2. **Staging 环境用于完整测试**
   - 运行完整 E2E 测试套件
   - 性能测试、安全扫描
   
3. **Production 只部署稳定版本**
   - 必须经过 Staging 完整验证
   - 使用 Git tags 版本管理

### ⛔ 禁止模式

1. **禁止跳过 Staging 直接到 Production**
   - 即使是"简单修复"也必须经过 Staging

2. **禁止在 Production 测试新功能**
   - 使用 Feature flags 控制灰度发布

3. **禁止共享数据库**
   - Test/Staging/Production 必须使用独立数据库

---

## 10. The Proof (验证方法)

### 验证环境隔离

```bash
# 验证容器名隔离
docker ps | grep finance_report
# 应该看到:
# - finance_report-postgres-pr-123 (Test)
# - finance_report-postgres-staging (Staging)
# - finance_report-postgres (Production)

# 验证域名隔离
curl https://report-pr-123.zitian.party/api/health
curl https://report-staging.zitian.party/api/health
curl https://report.zitian.party/api/health

# 验证数据隔离
ls /data/test/finance_report-pr-123/
ls /data/finance_report/staging/
ls /data/finance_report/
```

### 验证 0 帧起手部署

```bash
# 模拟全新 VPS
export DEPLOY_ENV=test
export ENV_SUFFIX=-pr-999
export PR_NUMBER=999

# 计时开始
time invoke fr-app.setup

# 预期结果:
# - 完成时间 < 5 分钟
# - 自动创建 bucket
# - 自动生成密钥
# - 健康检查通过
```

---

## 11. 未来规划

### 11.1 Multi-region Production

考虑未来支持多地域生产环境：
- `production-sg` (新加坡)
- `production-us` (美国)

### 11.2 Preview Environments

为每个 feature branch 自动创建预览环境：
- `report-feature-dark-mode.zitian.party`

---

*Last updated: 2026-01-20*
