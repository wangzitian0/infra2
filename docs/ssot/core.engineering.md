# Infra2 工程与文档准则

## 🧭 Wiki 入口地图与导航契约

> #765 把这几节从 `AGENTS.md` 移出时，提交信息写的是「move the procedures to SSOT」，
> 但它们没有落到任何文件里——全仓搜索只剩 `tools/README.md` 一处历史引用，而
> `doc_link_check.py` 正是以「互引原則」为存在理由。此处按该提交声明的去向补回。
> 路径相对本文件（`docs/ssot/`）。

**0 级入口**：[`AGENTS.md`](../../AGENTS.md)

**1 级入口（按用途）**
1. **全局工程概览** → [README.md](../../README.md)
2. **新手/应用接入** → [docs/onboarding/README.md](../onboarding/README.md)
3. **技术真理/架构规范** → [docs/ssot/README.md](README.md)
4. **项目追踪/进行中任务** → [docs/project/README.md](../project/README.md)

**补充入口**：**文档索引** → [docs/README.md](../README.md)

**阅读顺序（10 分钟速览）**
1. [README.md](../../README.md)（全局概览与命令）
2. [docs/onboarding/README.md](../onboarding/README.md)（场景路径）
3. [docs/ssot/README.md](README.md) → 先读 [core.md](core.md)，再读本文与
   [ops.merge-gate.md](ops.merge-gate.md)
4. [docs/project/README.md](../project/README.md)（绑定当前项目）

**路由规则（遇到问题进哪里）**
- 要上线/接入应用 → [Onboarding](../onboarding/README.md)
- 要改基础设施/服务 → Layer README（[bootstrap](../../bootstrap/README.md)、
  [platform](../../platform/README.md)、[tools](../../tools/README.md)）→ [SSOT](README.md)
- 要找规范/权威定义 → [SSOT](README.md)；工程与文档准则在本文，合流门禁在
  [ops.merge-gate.md](ops.merge-gate.md)
- 要找当前任务 → [Project](../project/README.md)
- 要改监控/告警目标 → 派生自服务注册表（`libs/service_registry.py` + 各 `deploy.py` 的
  `prod_only`），**禁止**手维护与 IaC 平行的服务清单。一致性校验见
  [`tools/watchdog_consistency_audit.py`](../../tools/watchdog_consistency_audit.py)；
  信号真源 [`watchdog-signals.yaml`](watchdog-signals.yaml)。

**互引原則**
- 0/1 级入口之间必须互相引用，避免单点入口遗失。这条是
  [`tools/doc_link_check.py`](../../tools/doc_link_check.py) 存在的理由，它只验链接可达，
  互引是否齐全仍靠人看。
- 这条曾被违反过一次，值得记：#765 删掉本条时，`AGENTS.md` 同时丢了指向仓库根部
  `README.md` 的链接——六个入口文档之间 30 条有向边只剩 29 条，缺的正是本条要防的那一条。
  由 #773 补回。当前状态：**30/30 成立**。

## 代码风格

- **DRY**：避免重复代码，使用函数/类/模块/组件等抽象。
- **避免魔法数字**：使用常量/枚举/配置文件等替代。
- **尽可能复用已有的库**：动手前永远先检查 libs 目录 [README.md](../../libs/README.md)。
- **不造新轮子（收敛红线，#542/#543）**：新增告警路径必须注册 signal（`tools/no_new_wheels_lint.py` 阻断 CI）；新增常驻监视 = probe-runner 的 `ResidentWatcher` 插件（`libs/resident_watchers.py`），不新建 sidecar/compose 服务；新增定时 ops 检查挂 `ops-checks.yml` 并声明 `# signal:`；服务级运维事实（探针/信号/备份/密钥）只声明在该服务 `deploy.py` 的 Facet 上，由注册表派生，不另开清单。入口全景见 [tools/README.md](../../tools/README.md)。

## 文档准则

### 文档归类

四类文档各有侧重；记录顺序为 Project → Layer README → SSOT → 开发者体验，逐步要求更严格精炼。

| 分类 | 路径 | 用途 | 适合人群 |
|------|------|------|---------|
| **Project** | `docs/project/` | 项目追踪，任务管理 | AI / 维护者 |
| **Layer README** | 各目录 `README.md` | 目录介绍，设计和维护指南 | 基础设施维护者 |
| **SSOT** | `docs/ssot/` | 复杂话题集中管理，技术参考手册 | 所有人 |
| **开发者体验** | `docs/onboarding/` | 场景驱动，注重接入顺滑 | 应用开发者 |

> README 模板见 [docs/README_tempate.md](../README_tempate.md)。
> `docs/ssot/README.md` 由 `docs/ssot/MANIFEST.yaml` 生成，改索引文字要改 MANIFEST 而不是 README。
> `docs/project/README.md` 同理，由各 `Infra-NNN` 文档自己的 H1 与 Status 行生成
> （`tools/gen_project_index.py`）——改索引要改那份文档，不是改索引。

**守卫必须能被它要守的改动触发。** 这两个索引的输入全是文档，所以证明它们没过期的门禁
**跑在 `docs.yml` 里**：它按 `**/*.md` 触发。infra-ci 里那两份同样的门禁待在
`has_non_doc == 'true'` 的 job 下，而「新增/改名/改 Status 一篇 Project 文档」「手改生成块」
恰恰是全 Markdown 的改动——门禁会被它存在的理由跳过。#505 那次漂移（Infra-004/005 索引里
状态错、Infra-010/011/014/016 整个缺失）今天原样重来会一路绿灯。判据不是「这个检查重不重要」，
而是**它的输入会不会在它不运行的那类 PR 里变**；会，就得挪到那类 PR 触发得到的地方。
证明见 `libs/tests/test_docs_only_prs_run_their_own_gates.py`：它按（是否 Markdown × 是否在
`docs/` 下）分象限，对每种改动形状断言至少有一个会触发的 workflow 跑了这两个生成器。

### AI 文档行为约束

1. **不随意生成文档**：需要记录的内容集中放入对应 `Infra-XXX.TODOWRITE.md`（同编号）。按 Scope Model 建立的载体（skill 目录、子树规则文件）不在此限。
2. **Project 文件配对**：每个 Project 含 `Infra-XXX.<project>.md` 与 `Infra-XXX.TODOWRITE.md`（同编号）。归档后合并为单文件（见 `docs/project/README.md`）。
3. **Project 目录规则**：`docs/project/archive/Infra-001.bootstrap_setup.md` 未经授权只可以加东西不可以删东西；artifacts 简要记录到对应 `Infra-XXX.TODOWRITE.md`，不随意创建新 Project 文件。
4. **每次修改必须更新**：改代码 → 更新对应目录 `README.md`；涉及架构变更 → 更新相关 SSOT 文档。

> 项目编号以 `docs/project/README.md` 为准；若本节与当前项目结构不一致，以它为准并同步修正本文。

## STAR 问题解决框架

处理任务前用以下级联结构做深度分析。

**1. Situation（情境评估）**
- **锚定 Project**：在 `docs/project/` 中绑定一个 project。
- **现状分析**：描述当前系统状态及问题影响。
- **真理检查（Step 0）**：搜索并阅读 `docs/ssot/` 中相关话题，明确"现状"与"理想真理"的差距。
- **工具发现**：编写新代码前**必须**先搜 `libs/` 与 `tools/` 是否已有实现（如 `grep -r "dokploy" libs/ tools/`）。已有库存在时禁止重复造轮子。

**2. Tasks（多维任务拆解）**
- **目标分拆**：根据 Situation 拆出多个子任务。
- **按层归位**：分发到对应基础设施层级（例：Task 1 (L1) 扩容磁盘；Task 2 (L3) 迁移数据）。

**3. Actions（具体执行步骤）**
- **原子操作**：为每个 Task 制定具体 Action 序列。
- **SSOT 对齐**：Actions 必须符合 [Ops Standards](ops.standards.md) 的防御性守则。
- **闭环变更**：必须包含 修改代码 → 更新 SSOT → 验证生效。

**4. Result（结果验证）**
- **完工自检**：对照 `docs/project/` 中的 Project 检查。未完成记录 todo，完成则移入 `docs/project/archive/`。
- **证据闭环**：通过相关 SSOT 文档 "The Proof" 章节定义的测试证明结果。
- **更新文档**：更新所在目录 README、Project 文档、SSOT 文档。

## 运营准则

- **工具优先级**：mcp > cli > api > ssh > web 浏览器。
- **drift 修复**：可以手动创建测试或修补线上问题，但必须确保代码修复，合并后 apply 一次消除 drift。
- **0 宕机原则**：有宕机风险必须主动提出。若必须宕机，须提出降低宕机时长的方案。
