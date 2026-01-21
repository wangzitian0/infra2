# Infra-010: IaC & Service Verification

> **状态**: In Progress  
> **开始时间**: 2026-01-21  
> **目标**: Verify all Dokploy services are IaC-managed, fix broken services, validate post-merge CI, ensure production health, document complete integration SOP

---

## 📋 任务清单

### ✅ 已完成

1. **IaC Runner 根因分析**
   - 确认 IaC Runner 容器崩溃原因：缺少 vault-agent sidecar 导致无法获取 `GIT_REPO_URL`
   - Vault 中存在密钥，但容器无法访问

2. **IaC Runner 修复方案**
   - 创建 PR #74 添加 vault-agent sidecar pattern
   - 修改文件：
     - `bootstrap/06.iac-runner/vault-agent.hcl` (新建)
     - `bootstrap/06.iac-runner/secrets.ctmpl` (新建)
     - `bootstrap/06.iac-runner/vault-policy.hcl` (新建)
     - `bootstrap/06.iac-runner/compose.yaml` (修改)
     - `bootstrap/05.vault/tasks.py` (修改 - 添加 iac_runner 到 setup-tokens)
   - PR 已关闭 (closed by user)

3. **新服务 SOP 文档**
   - 创建 `docs/onboarding/07.new-service-sop.md` (4个核心步骤)
   - 已更新 `docs/onboarding/README.md` 添加 SOP 链接

4. **服务清单核查**
   - 已确认 13 个 IaC-managed 服务 (有 deploy.py)
   - 已确认生产容器运行状态

### 🔄 进行中

5. **IaC Runner 手动部署**
   - PR #74 已关闭，compose.yaml 未应用
   - 需要手动执行 `invoke iac-runner.setup`

### ⏳ 待办

6. **Post-Merge CI 验证**
   - 验证 idempotent scripts 在 post-merge CI 中正常执行
   - 当前 `.github/workflows/infra-ci.yml` 仅做 lint 和 validate，**不自动部署**

7. **生产服务健康检查**
   - 完整健康检查表（包含所有服务）
   - 验证 IaC Runner 修复后的健康状态

8. **集成文档完善**
   - 补充 post-merge automation 文档
   - 补充 IaC Runner value proposition 文档

---

## 🔍 发现 (Findings)

### IaC 管理服务清单 (13个)

| Layer | Service | Status | Deploy Path |
|-------|---------|--------|-------------|
| **Bootstrap** | iac-runner | ❌ 需修复 | `bootstrap/06.iac-runner/deploy.py` |
| **Platform** | postgres | ✅ | `platform/01.postgres/deploy.py` |
| **Platform** | redis | ✅ | `platform/02.redis/deploy.py` |
| **Platform** | clickhouse | ✅ | `platform/03.clickhouse/deploy.py` |
| **Platform** | minio | ✅ | `platform/03.minio/deploy.py` |
| **Platform** | authentik | ✅ | `platform/10.authentik/deploy.py` |
| **Platform** | signoz | ✅ | `platform/11.signoz/deploy.py` |
| **Platform** | portal | ✅ | `platform/21.portal/deploy.py` |
| **Platform** | activepieces | ✅ | `platform/22.activepieces/deploy.py` |
| **Finance Report** | fr-postgres | ✅ | `finance_report/finance_report/01.postgres/deploy.py` |
| **Finance Report** | fr-redis | ✅ | `finance_report/finance_report/02.redis/deploy.py` |
| **Finance Report** | fr-app | ✅ | `finance_report/finance_report/10.app/deploy.py` |
| **Finance** | wealthfolio | ⚠️ 待验证 | `finance/wealthfolio/deploy.py` |

### 生产服务健康状态 (2026-01-21)

| Service | Health Endpoint | Status | Notes |
|---------|----------------|--------|-------|
| Finance Report | `https://report.zitian.party/api/health` | ✅ healthy | Production OK |
| Vault | `https://vault.zitian.party/v1/sys/health` | ✅ unsealed (v1.15.4) | |
| Authentik | `https://sso.zitian.party` | ✅ HTTP 302 | |
| MinIO | `https://minio.zitian.party` | ✅ HTTP 200 | |
| SigNoz | `https://signoz.zitian.party` | ✅ HTTP 200 | |
| Portal | `https://home.zitian.party` | ✅ HTTP 302 | |
| Activepieces | `https://automate.zitian.party` | ✅ HTTP 302 | |
| **IaC Runner** | `https://iac.zitian.party/health` | ❌ 404 | **需修复** |
| Dokploy | `https://cloud.zitian.party` | ✅ HTTP 200 | |

### IaC Runner 核心价值定位

**非触发器角色**：IaC Runner 的核心价值 **不是** 触发部署 (Dokploy 的 GitHub integration 已处理)

**实际价值**：
1. `pre_compose` hook: SSH 到 VPS 创建目录、设置权限
2. Config hash detection: 仅在配置实际变化时重新部署
3. Vault secrets automation: 首次部署时自动生成密码
4. Environment variable injection: 自动更新 Dokploy 环境变量

### Post-Merge CI 现状

**当前行为**：
- `.github/workflows/infra-ci.yml` 仅在 PR 和 push to main 时执行
- **不包含自动部署**：只做 validate compose + lint Python
- `invoke` 脚本 **不会** 在 CI 中自动执行

**缺失的 Post-Merge Automation**：
- IaC Runner webhook 应该在 merge to main 后触发 `invoke {service}.sync`
- 目前 IaC Runner 挂了，所以这个环节断了

---

## 📝 技术细节

### IaC Runner 问题诊断

**症状**：
```
RuntimeError: GIT_REPO_URL environment variable must be set
```

**根因**：
- compose.yaml 缺少 vault-agent sidecar
- Vault 有密钥 (`WEBHOOK_SECRET`, `GIT_REPO_URL`)，但容器无法访问
- 容器环境变量 `VAULT_APP_TOKEN=` (空值)

**解决方案** (已在 PR #74，但未应用)：
- 添加 vault-agent sidecar (与其他服务一致)
- 修改 entrypoint 等待 `/secrets/.env` 被 vault-agent 渲染
- 在 `vault.setup-tokens` 中注册 `iac_runner` 服务

### 服务发现机制

`libs/deployer.py` 中的 `discover_services()` 基于文件系统：
```python
# 扫描规则
platform/**/deploy.py → <service>.sync
finance_report/**/deploy.py → fr-<service>.sync
bootstrap/**/deploy.py → <service>.sync
```

### 新服务 SOP 核心步骤

1. **代码结构验证** - 7个必需文件
2. **Vault Secrets 配置** - env.set + setup-tokens
3. **IaC 集成验证** - discover_services()
4. **部署验证** - invoke setup + status
5. **线上健康检查** - curl health endpoint
6. **Post-Merge CI 验证** - IaC Runner webhook
7. **文档更新** - README + SSOT

---

## 🎯 下一步行动

### 立即执行 (Manual)

1. **修复 IaC Runner**
   ```bash
   cd /path/to/infra2
   export VAULT_ROOT_TOKEN=$(op read 'op://Infra2/.../Token')
   invoke vault.setup-tokens  # 生成 VAULT_APP_TOKEN
   invoke iac-runner.setup     # 应用新 compose.yaml
   curl https://iac.zitian.party/health  # 验证
   ```

2. **测试 Post-Merge 流程**
   ```bash
   # 推送一个小变更到 main
   # 观察 IaC Runner 是否触发 sync
   ssh root@$VPS_HOST "docker logs iac-runner -f"
   ```

### 文档完善

3. **补充 SSOT**
   - 在 `docs/ssot/ops.pipeline.md` 补充 IaC Runner 工作流
   - 明确 "GitHub webhook → IaC Runner → invoke sync" 链路

4. **补充 Onboarding**
   - 在 `07.new-service-sop.md` 的 "Post-Merge CI 验证" 章节补充：
     - IaC Runner webhook 配置验证
     - 手动触发 sync 的方法
     - 预期日志输出

---

## 🔗 相关文档

- [New Service SOP](../onboarding/07.new-service-sop.md)
- [IaC Runner README](../../bootstrap/06.iac-runner/README.md)
- [Platform Automation](../ssot/platform.automation.md)
- [Ops Pipeline](../ssot/ops.pipeline.md) (待补充)

---

## 📊 验证标准

项目完成条件：

- [ ] 所有 Dokploy 服务都有对应的 `deploy.py` (IaC-managed)
- [ ] 所有生产服务健康检查通过 (包括 IaC Runner)
- [ ] Post-merge CI 能正确执行 idempotent scripts (通过 IaC Runner)
- [ ] 新服务 SOP 文档完整 (已有，待补充 post-merge 部分)
- [ ] 集成文档完善 (IaC Runner value + workflow)

---

*Last updated: 2026-01-21*
