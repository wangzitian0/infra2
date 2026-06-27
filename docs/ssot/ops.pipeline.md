# Delivery SSOT (CI/CD · 环境触发 · 部署 · 发布)

> **SSOT Key**: `ops.pipeline`
> **核心定义**: infra2 作为**独立产物**的交付契约——CI/CD、**环境触发模型**、`deploy_v2`
> 前门、tag 驱动的 reconcile、以及**发布机制**。这是「**什么触发什么、什么是 live、怎么发布**」的
> **唯一权威**;环境本身的定义（六环境/域名/隔离/遥测标识）见
> [core.environments.md](./core.environments.md)。
>
> **心智模型（与 app 不同）**:
> - **app 是消费者**——部署 = 选一个自己的镜像版本跑在平台上;手动或 IaC 触发**都符合预期**。
> - **infra2 是发布型产物**——**一个 git tag = 一个平台发布候选**:推送即**自动晋升 staging**,
>   **prod 需显式 promote**后才成为权威 live 版本(打 tag ≠ 动 prod,§3.3)。
>   `main` 领先于最近 tag = **未发布**(正常,不是 drift)。

---

## 1. 真理来源 (The Source)

| 维度 | 物理位置 (SSOT) | 说明 |
|------|----------------|------|
| **IaC Input Reconcile** | [`reconcile-iac-inputs.yml`](https://github.com/wangzitian0/infra2/blob/main/.github/workflows/reconcile-iac-inputs.yml) | **Release-tag** 触发(`push: tags: v*.*.*`):diff 上一 tag→本 tag,把 changed `iac_pinned` 服务以**该 tag** 为 `iac_ref` fan-out 给 `deploy_v2 → iac_runner`;config-hash gate 决定 no-op vs 重启。**不是 main-push、不是 sha。** |
| **Deploy 前门 (`deploy_v2`)** | [`tools/deploy_v2.py`](https://github.com/wangzitian0/infra2/blob/main/tools/deploy_v2.py) · [`deploy.yml`](https://github.com/wangzitian0/infra2/blob/main/.github/workflows/deploy.yml) | 统一部署坐标 `(service, type, version_ref, iac_ref)`;app + 平台、staging/prod、pinned ref。 |
| **IaC Runner Bootstrap (L1)** | [`deploy.yml`](https://github.com/wangzitian0/infra2/blob/main/.github/workflows/deploy.yml) · [`scripts/deploy_iac_runner_bootstrap.sh`](https://github.com/wangzitian0/infra2/blob/main/scripts/deploy_iac_runner_bootstrap.sh) | **带外**自更新:`bootstrap/06.iac_runner/**` 变更时,Actions 在 VPS 上重建 runner 自身(跟 merged SHA),**独立 cadence**。 |
| **Auto-deploy report-branch-main** | [`deploy-report-main.yml`](https://github.com/wangzitian0/infra2/blob/main/.github/workflows/deploy-report-main.yml) | **唯一**自动目标:app main push → main 预览重部署。 |
| **Observability config apply** | [`apply-observability.yml`](https://github.com/wangzitian0/infra2/blob/main/.github/workflows/apply-observability.yml) | 告警规则 / 看板,声明式 reconcile。**当前 merge 即 apply**(见 §3.4 未收口项)。 |
| **Docs site** | [`docs.yml`](https://github.com/wangzitian0/infra2/blob/main/.github/workflows/docs.yml) · [`docs/mkdocs.yml`](../mkdocs.yml) | MkDocs → GitHub Pages。 |

---

## 2. 部署目标与触发 (Deploy targets & triggers) — 单一真理

> 这是「什么触发什么」的**唯一权威**。环境**定义**(每个环境是什么、域名、隔离)在
> [core.environments.md](./core.environments.md);此处只讲**触发与交付**。

### 2.0 三过程,三闸门(解耦)

「开发 / 测试 / 发布」是三个**各自优化**的过程,用**不同触发器 + 不同闸门**解耦,互不拖累:

| 过程 | 目标 | 触发 | 闸门(放行条件)| 碰 prod? |
|------|------|------|----------------|----------|
| **开发** | 快 | PR / push | **CI(lint+单测+E2E)绿即可 merge**;merge **绝不**触发真实 staging/prod 部署 | 否 |
| **测试** | 提前检测 | PR CI 内 | **reconcile `--dry-run` plan**:此改动一旦发布会重部署哪些 `iac_pinned` 服务 + plan 能否 build(fan-out/契约/import 在 PR 就 resolve) | 否 |
| **发布** | prod 稳 | release tag | **分级 + fail-closed**:tag→自动 staging(soak);prod→**显式 promote**;来源守卫(on-main,§6)+ `--staging-validated` + `--code-reviewed` | 是,**显式** |

三条解耦铁律:**「merge」≠「部署」**(开发快)· **部署前校验在 PR 就跑**(测试提前)· **「打 tag」≠「动 prod」**(发布稳)。
因为 `deploy_v2` 是 app + platform 的统一前门,所有部署侧守卫都在此收口。

**核心铁律**:
- `report-branch-main`(main 预览)随 main 合并**自动**重部署——「永远看到 main 现在长啥样」的活环境。
- **staging / prod 只部署 release tag**(不可变镜像,不接受 branch/sha);staging 与 prod **钉同一个 tag**
  (**promote-not-rebuild**,不是部署 main sha)。
- 统一入口 `tools/deploy_v2.py`;**prod 对真实数据 deny-by-default**,缺 `--staging-validated --code-reviewed` 直接 fail-closed。

| 目标 | 触发 | 部署什么 | 入口(示意)| 数据 | 生命周期 |
|------|------|---------|------|------|---------|
| **report-branch-main**(preview)| **自动**(main 合并即发)| main 尖端 | `deploy_v2 --type preview/branch` | 临时 DB | teardown 前 |
| **其余 preview**(pr/commit/tag)| 手动按需 | PR# / sha / tag | `deploy_v2 --type preview/{pr,commit,tag}` | 临时 DB | teardown 前 |
| **app staging** | **手动**(app)| release tag(钉和 prod 同一个)| `deploy_v2 --type staging --version-ref vX.Y.Z` | staging 数据 | 长期(同构 prod)|
| **app prod** | **手动**(app)| **同一个 release tag** | `deploy_v2 --type prod --version-ref vX.Y.Z --staging-validated --code-reviewed` | 真实 prod 数据 | 长期 |
| **平台服务**(iac_pinned)**staging** | **release tag 推送**(`v*.*.*`)→ **自动 staging**(soak)| **该 tag** 作 `iac_ref` | `reconcile-iac-inputs.yml` → `deploy_v2 → iac_runner` | — | 长期 |
| **平台服务**(iac_pinned)**prod** | **显式 promote**(`workflow_dispatch` `promote_prod=true` / `--promote-prod`)——tag 推送**不**自动动 prod | **同一 tag** | 同上 | — | 长期 |
| **L1 Bootstrap**(iac-runner)| `bootstrap/06.iac_runner/**` 变更 | merged SHA | 带外 self-update(`deploy.yml`)| — | 独立 cadence |

> **平台服务**(iac_pinned)无 preview,只有 staging/prod,且**只接受 release tag 作 `iac_ref`**。release tag
> 推送后 `reconcile-iac-inputs.yml` 自动:diff 上一 release tag → 本 tag,changed files 经
> [`deploy-dependencies.yaml`](./deploy-dependencies.yaml) fan-out 到受影响 `iac_pinned` 服务,以**该 tag** 触发
> `deploy_v2 → iac_runner`:**自动只晋升 staging**(soak);**prod 由显式 promote**(`workflow_dispatch
> promote_prod=true` / `--promote-prod`)用同一 tag 放行——**tag 推送不自动动 prod**。是否重启由 Deployer
> config-hash gate 定(hash 未变即 no-op)。
> CI(lint + 单测 + E2E)**不**触发 staging/prod 部署;reconcile 是独立的 tag-triggered apply。
> **来源守卫**:apply 前 `assert_after_on_main` fail-closed 校验该 tag reachable from `origin/main`;
> off-main / 未合并 tag 直接拒绝(见 §6)。这把「打 tag」与「能动 prod」绑定到 reviewed main——
> 在 feature 分支打 tag 不再等于动 prod(解耦的第一步)。

---

## 3. 发布机制 (Release model) — infra2 是独立产物

### 3.1 发布单位 = 一个 git tag

**一个 `v*.*.*` tag = 一次平台发布 = 权威的「应该 live 的平台版本」。** 语义化版本:

- **MAJOR**: 破坏性/需手工迁移的基础设施变更(罕见)。
- **MINOR**: 新能力。
- **PATCH**: 修复。

semver 当**风险信号**:一眼知道这次发版多险、prod 前要不要多 soak。`main` 领先于最近 tag =
**未发布**(对独立产物完全正常),**不报 drift**。

### 3.2 分层 cadence(为什么不是「一个 tag 部署一切」)

infra2 有天然分层(层级编号沿用 [core.md#层级定义](./core.md#层级定义):**L1 Bootstrap / L2 Platform**),
**不同层/同层不同面有意走不同 cadence**——这不是 drift,是**自举决定的结构**:

| 层 | 内容 | 部署触发 | 跟什么版本 |
|----|------|---------|-----------|
| **L1 Bootstrap** | iac-runner / Vault / Dokploy(部署引擎自己)| **带外自更新**(它就是执行 reconcile 的人,不能等自己的 tag)| merged SHA |
| **L2 Platform · 服务** | postgres / redis / signoz / alerting / openpanel … | **release tag → reconcile** | release tag |
| **L2 Platform · 配置** | 告警规则 / 看板 / 探针 spec | **当前 merge 即 apply**(目标:折进 tag reconcile,见 §3.4)| merge SHA(目标:tag)|

→ **「最近 tag」是 L2 Platform·服务 的权威 live 版本**;L1 Bootstrap 与 L2·配置 的版本错位**必须可见**(§3.5),不静默、也不叫 drift。

### 3.3 晋升与 soak(promotion)

release tag 推送**自动晋升 staging**(promote-not-rebuild);**prod 是显式的、单独触发的晋升**
(`workflow_dispatch promote_prod=true` / `reconcile --promote-prod`),**绝不随 tag 自动**。安全垫由此成为
强制时序而非约定:**staging 先收敛 → soak → 人为放行 prod**(同一 tag)。高 MAJOR/风险发布 soak 更久。
这把「打 tag」和「能动 prod」解耦——在未合并 feature 分支打 tag 既被来源守卫拒(§6),即便在 main 上也只自动到
staging。(RC tag `v*.*.*-rc.N` + soak 窗口仍为目标态,见 §3.4。)

### 3.4 未收口项(open / 目标态)

1. **把 `apply-observability` 折进 tag reconcile** —— 当前 L2 配置 merge 即 apply,使「一个 tag」不是平台 live 的
   **完整**快照。**前置依赖:[Infra-013](../project/Infra-013.service_registry_ssot.md) P1**(从 registry 生成
   `watchdog-signals.yaml` / `INFRA_PROBE_SPECS`,消除手抄)——在 P1 完成前,折进 tag 是脆的。
2. **Release-train 自动化**(release-please 式):常驻 `Release vX.Y.Z` PR 累积「未发布增量」+ 自动 changelog +
   自动 semver。它的 diff 就是「哪些没发布」的答案(替代靠人 ssh 手查)。
3. ✅ **显式晋升闸门已落地**:tag 自动 staging,prod 经 `--promote-prod` 显式放行(reconcile 拆分 + 来源守卫)。
   **仍为目标态**:RC tag `v*.*.*-rc.N` + 自动化 soak 窗口/时长闸门(Infra-011 目前只有时间预算,无自动 soak 判定)。

### 3.5 各层 running vs tag/main 可见性(目标态)

一条 check 显示 `L1-Boot=sha_x | L2-服务=v1.1.10 | L2-配置=sha_y` vs `main` —— 任何版本错位**永远可见**,
主动判断「要不要发布」,而不是被动追平或靠手查。

---

## 4. `deploy_v2` 坐标(部署前门 SSOT)

> **设计参考**: [Infra-015 deploy_v2 front door](../project/Infra-015.deploy_v2_front_door.md)(EPIC 追踪;**契约以本文为准**)。

统一坐标 **`(service, type, version_ref, iac_ref)`**——四个正交轴:

- `service`: `service_registry` key(如 `platform/postgres`、`finance_report/app`)。
- `type`: `preview/{branch,pr,commit,tag}` / `staging` / `prod`。
- `version_ref`: 多态——PR# / sha / **tag** / branch / release(app 业务镜像身份)。
- `iac_ref`: **钉 infra2 栈修订**(staging/prod 只接受 tag)。

prod red lines 由 `deploy_v2` 强制(deny-by-default):`--staging-validated` + `--code-reviewed`,否则 fail-closed。

---

## 5. IaC Runner 集成

> **详细参考**: [bootstrap.iac_runner SSOT](./bootstrap.iac_runner.md)

IaC Runner 是 **L1 Bootstrap 层**组件,自动化部署 **L2 Platform 层**服务(层级编号见 [core.md#层级定义](./core.md#层级定义))。

post-merge 部署被 GitHub Actions `concurrency` 串行化,调 IaC Runner 前先 `/health` preflight(bootstrap drift
在任何签名 `/deploy` 前先 fail)。Actions 用短签名 `/deploy` 启动、再轮询签名 `/deploy/status`——**绿 workflow =
IaC Runner 报告了完成的服务 sync 结果,不只是请求被接受**。不走公网 Cloudflare 的 `wait=true`(会在 sync 完成前 524)。

### 5.1 Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | 健康检查(含 vault / op / dokploy_api_key 功能性 fail-closed)|
| `/webhook` | POST | GitHub webhook(change-based sync)|
| `/deploy` | POST | 版本部署;`wait=true` 为 legacy 直等 |
| `/deploy/status` | POST | 签名状态轮询,取真实 sync 结果 |
| `/sync` | POST | legacy 手动同步;默认关闭,由 `ENABLE_LEGACY_SYNC` gate 开启 |

### 5.2 服务映射规则(fan-out)

| 变更路径 | 触发 | 说明 |
|---------|------|------|
| `platform/<nn>.<svc>/*` | `<svc>.sync` | 自动同步该平台服务 |
| `libs/*` | **全平台服务** | 公共库变更,全量同步 |
| `bootstrap/06.iac_runner/*` | **带外 bootstrap 更新** | Actions 在 `/deploy` 前重建 runner |
| `bootstrap/*` | **Skipped** | 其余 bootstrap 是首装/灾备依赖,保持手动 |
| `finance_report/*` · `finance/*` | **Skipped** | 走应用独立 CI |

**为什么 runner bootstrap 走带外**:IaC Runner 不能在自己正被 Actions 轮询的 `/deploy` 请求里重启自己;Actions
从容器外拥有自更新步骤(更新 Dokploy compose checkout → 重建 runner 镜像 → 等 health → 再 `/deploy`)。

### 5.3 幂等性保证(config-hash gate)

每个服务 `sync` 用 config hash:`SHA256(compose.yaml + env vars + local artifacts)` vs Dokploy 存的 `IAC_CONFIG_HASH`,
仅 hash 不匹配时重部署。local artifacts 含 compose 引用的 bind-mount 文件、Dockerfile 及其 `COPY`/`ADD` 源——
防止代码/Vault 模板变了但 compose 文本没变时被跳过。

**Dokploy 接受 deploy 请求 ≠ runtime 真变了**:每次 generic compose deploy 后,Deployer 记录现有 deployment IDs,
要求出现新的 `running`/`done` 记录,`compose.deploy` 为 no-op 时用 `compose.redeploy` 重试一次,两次都没新记录则 sync 失败。

### 5.4 Env × Stage Result Contract

`libs/pipeline_stage_contract.py` 拥有 CI/CD、route canary、watchdog、probe 共享的稀疏 Env×Stage 证据 schema。
当 stage 结果用于部署决策/告警路由/加速时,producer 必须发可比记录而非一次性日志。

必填:`source` · `environment`(`local`/`pr`/`pr-preview`/`staging`/`production`)· `stage` · `target` ·
`status`(`pass`/`fail`/`skip`/`warn`/`running`)· `duration_ms`/`deadline_ms` · `failure_domain`(失败必填)·
`external_dependency` · `suppressed_reason`/`skipped_reason`(跳过必填,使加速可审计)。

**加速规则**:prod 跳过不算安全加速;非 prod 跳过仅当 stage 显式 eligible 且带 reason 才算。
**一致性规则**:跨 stage 矛盾记为 disagreement kind(如内部 health 绿但公网路由挂、heartbeat 与公网路由不符)。

---

## 6. 设计约束 (Dos & Don'ts)

### ✅ 推荐
- staging/prod **只部署 release tag**;prod 从 staging 已验证的**同一 tag** promote(promote-not-rebuild)。
- 用语义化版本;semver 当风险信号。
- 平台层「什么是 live」= 最近 release tag;L1 Bootstrap 与 L2·配置 的错位走可见性视图(§3.5),不当 drift。

### ⛔ 禁止
- 禁止部署 untagged commit / branch / sha 到 staging/prod。
- **禁止部署 off-main tag**:能驱动 staging/prod 部署的 tag,其 commit **必须 reachable from `origin/main`**。
  reconcile 入口由 `assert_after_on_main` **fail-closed** 强制(Infra-011 不变式:*iac_pinned prod
  reconcile 只能来自 reviewed main*)。在未合并 feature 分支上打 release tag 不会再打穿 prod
  (v1.1.16 事故根因)。`--dry-run` 仅做 plan,豁免此校验。
- **禁止 tag 推送自动部署 prod**:prod 必须经**显式 promote**(`promote_prod=true` / `--promote-prod`);
  tag 只自动晋升 staging。「打 tag」与「动 prod」必须解耦。
- 禁止跳过 staging 直接 prod。
- 禁止手改 production 服务配置(必须经 GitOps)。
- 禁止手推 `gh-pages`(统一 Actions 发布)。
- 禁止把「`main` 领先于最近 tag」当 drift——那是未发布(正常)。

---

## 7. Hotfix 流程

从 production tag 拉分支修复,patch +1。**hotfix 仍必须经 reviewed main**——来源守卫(§6)拒绝 off-main tag,
所以「不合并回 main 直接打 tag」的老做法会被 fail-closed 挡掉(那正是 v1.1.16 类隐患的后门)。fast-track:

```bash
git checkout v1.3.0 && git checkout -b hotfix/critical-fix
git commit -am "fix: critical issue"
gh pr create --base main --fill          # fast-track review → 合并回 main(on-main 不变式)
# 合并后,从 main 打 patch tag → 自动晋升 staging:
git checkout main && git pull && git tag v1.3.1 && git push origin v1.3.1
# staging soak 后,显式 promote 平台服务到 prod:
gh workflow run reconcile-iac-inputs.yml -f after=v1.3.1 -f promote_prod=true
# app 走统一前门、钉同一 tag(prod 仍 deny-by-default):
gh workflow run deploy.yml -f service="finance_report/app" -f type="prod" \
  -f version_ref="v1.3.1" -f iac_ref="v1.3.1" -f staging_validated=true -f code_reviewed=true
```

---

## 8. 验证与测试 (The Proof)

| 行为 | 验证方式 | 状态 |
|------|----------|------|
| **平台服务随 tag 晋升** | push `v*.*.*` → `reconcile-iac-inputs.yml` 运行 → IaC Runner logs | ✅ 本季实测(v1.1.9/v1.1.10)|
| **Config hash 幂等性** | 相同配置重复部署应 no-op | ✅ |
| **Env×Stage schema/speed/consistency** | `libs/tests/test_pipeline_stage_contract.py` | ✅ |
| **deploy_v2 prod fail-closed** | 缺 `--staging-validated/--code-reviewed` → 拒绝 | ✅ |
| **off-main tag 来源守卫** | `assert_after_on_main` 拒绝非 `origin/main` 祖先的 tag → `libs/tests/test_reconcile_iac_inputs.py` | ✅ |
| **tag 不自动 prod(staging/prod 拆分)** | tag 推送只 apply staging;prod 需 `--promote-prod` → `commands_to_apply` 测试 | ✅ |
| **测试提前(PR dry-run plan)** | PR CI `Gate reconcile plan builds`:dry-run 出 fan-out plan,不部署 → `.github/workflows/infra-ci.yml` | ✅ |

---

## 9. Troubleshooting

```bash
# 平台服务没随 tag 上 → 查 reconcile 是否在 tag 上 fire（不是 main push）
gh run list --workflow="Reconcile IaC Inputs" --event push
gh run view <run-id> --log

# IaC Runner
ssh root@103.214.23.41 "docker logs iac-runner --tail 100"
curl -i https://iac.zitian.party/health   # 404 = routing/app drift，不是坏 payload

# 版本 tags
git fetch --tags && git tag -l "v*.*.*" | sort -V | tail -5
```

---

## Used by

- [docs/ssot/README.md](./README.md)
- [docs/ssot/core.environments.md](./core.environments.md)(环境定义;部署触发指向本文)
- [docs/ssot/bootstrap.iac_runner.md](./bootstrap.iac_runner.md)
- [docs/project/Infra-015.deploy_v2_front_door.md](../project/Infra-015.deploy_v2_front_door.md)(EPIC;契约以本文为准)
