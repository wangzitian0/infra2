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

**守卫必须能被它要守的改动触发。** 这两个索引「是否同步」早就有测试证明
（`test_project_index_generated` / `test_ssot_index_generated`），它们跑在 infra-ci 的
`test-deployer-logic` 里——GitHub ruleset 强制的七个必需检查之一。缺陷从来不在这两个测试，
而在承载它们的 job 由 `has_non_doc` 门控：**新增/改名/改 Status 一篇 Project 文档、
手改生成块，全都是纯 Markdown**，于是「让索引过期的那种改动」恰好就是「跳过证明索引没过期的那个测试」
的那种改动。#505 是留在案的实例（Infra-004/005 状态错、Infra-010/014/016 整个缺失）。

所以判据是：**一份文档如果是某个生成物的输入，它就是配置，不是散文**——`detect-changes` 按这条
把 `docs/project/**.md` 与 `docs/ssot/README.md` 判为 non-doc（与 #774 对 `AGENTS.md`/`CLAUDE.md`
同一条理由）。规则刻意比生成器自己的输入集**粗**：生成器跳过 `*.TODOWRITE.md`/`*.SUMMARY.md`，
精确复述会在 shell 里留下第二份会漂移的真源。**决定权在漂移的方向**——粗规则是超集，漂了只是多跑一次
CI；精规则一旦跟不上就少认一个真输入，门禁直接哑掉。实测 300 个 PR（97 天）：精确版翻转 2 个，
粗版 5 个，代价是每季度三次约 2 分钟的 job。

**反面教材值得记住**：第一版不是这么做的，而是在 `docs.yml` 里加门禁步骤、再写 700 行测试去**模拟 CI**
证明那个步骤会跑且会咬人。六轮对抗审计用 **19 种**不同方式打穿它——step/job 的 `if:`、
`working-directory`、`strategy.matrix`、`container`、`services`、`concurrency`、`runs-on`、
`permissions`、`timeout-minutes`、`env`、`defaults`、checkout 的 `with: {ref: main}`、
`on.pull_request.types`、`outputs:` 里一个拼错的 step id……因为它的输入是 **GitHub Actions 的
schema**，那是个枚举不完的开集。**守卫的输入面比守卫的断言强度更决定成败**：
把问题缩成「一个布尔分类器对四条路径模式的真值表」，才是可判定的。
证明见 `libs/tests/test_generated_doc_indexes_are_guarded.py`——它执行 `detect-changes`
自己的 shell，并带一条对照（普通散文仍是 doc-only，否则规则宽到失去意义）。

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
