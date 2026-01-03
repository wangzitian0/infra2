# 基础设施 AI Agent 行为准则

> **禁令**：除非明确指定，否则 AI 不可以自动修改本文件。AI 不可以执行合流 (Merge PR) 操作。

## 🧭 Wiki 入口地图（0级/1级）

**0级入口**：`AGENTS.md`（你在这里）

**1级入口（按用途）**
1. **全局工程概览** → [README.md](README.md)
2. **新手/应用接入** → [docs/onboarding/README.md](docs/onboarding/README.md)
3. **技术真理/架构规范** → [docs/ssot/README.md](docs/ssot/README.md)
4. **项目追踪/进行中任务** → [docs/project/README.md](docs/project/README.md)

**补充入口**
- **文档索引** → [docs/README.md](docs/README.md)

**阅读顺序（10 分钟速览）**
1. [README.md](README.md)（全局概览与命令）
2. [docs/onboarding/README.md](docs/onboarding/README.md)（场景路径）
3. [docs/ssot/README.md](docs/ssot/README.md) → 先读 [docs/ssot/core.md](docs/ssot/core.md)
4. [docs/project/README.md](docs/project/README.md)（绑定当前项目）

**路由规则（遇到问题进哪里）**
- 要上线/接入应用 → [Onboarding](docs/onboarding/README.md)
- 要改基础设施/服务 → Layer README（[bootstrap](bootstrap/README.md), [platform](platform/README.md), [tools](tools/README.md)）→ [SSOT](docs/ssot/README.md)
- 要找规范/权威定义 → [SSOT](docs/ssot/README.md)
- 要找当前任务 → [Project](docs/project/README.md)

**互引原则**
- 0/1 级入口之间必须互相引用，避免单点入口遗失。

## ⚡ 快速理解路径（极简 6 步）

> **用途**：让第一次进入项目的人在 10 分钟内建立全局心智（目标导向）。

1. **确定目标**：用一句话写清“要达成的状态/结果”（不是角色视角）。
2. **选一个 Project 锚点**：从 `docs/project/README.md` 选择当前工作项（默认优先最新的 In Progress）。
3. **读 SSOT 索引**：从 `docs/ssot/README.md` 进入，先看 `core.md`，再补足与项目相关的话题。
4. **定位层级**：判断是 L1/L2/L3/L4 哪一层，避免跨层依赖与循环。
5. **做 Step 0 真理检查**：对比“现状 vs SSOT”，把差距记录到对应 TODOWRITE。
6. **闭环执行**：修改代码 → 更新 SSOT → 验证 → 更新 README / Project。

## 📌 入口与命名补充（以此为准）

1. **Project 选择/新增**：
   - 有合适的项目就选择它并绑定。
   - 没有合适的项目则新增一个（按前缀编号）。
2. **命名规则**：
   - **前缀要求**：`Infra-XXX`（数字编号）。
   - **文件名**：小写 + 下划线（例如 `Infra-004.authentik_install.md`）。
3. **SSOT = Wiki 核心**：
   - `docs/ssot/` 是事实真源与“主入口”。
   - `docs/README.md` 仅作为导航，不替代 SSOT。

## ✅ 极简 SOP（执行细则）

1. **写清目标**：一句话定义“完成标准”（可验证）。
2. **锁定 SSOT**：列出 1-3 个最相关的 SSOT 话题。
3. **列任务**：按层级拆分任务（L1/L2/L3/L4）。
4. **执行变更**：小步提交，保持可回滚。
5. **更新文档**：同步 Project / SSOT / README。
6. **验证闭环**：按 SSOT 的 “The Proof” 章节执行验证。


## 🛠️ 问题解决框架 (STAR Framework)

在处理任务前，AI 必须使用以下级联结构进行深度分析：

### 1. Situation (情境评估)
- **锚定Project**：在 `docs/project/` 中绑定一个project。
- **现状分析**：描述当前系统状态及问题影响。
- **真理检查 (Step 0)**：搜索并阅读 `docs/ssot/` 中的相关话题，明确“现状”与“理想真理”的差距。

### 2. Tasks (多维任务拆解)
- **目标分拆**：根据 Situation 拆解出多个子任务。
- **按层归位**：将 Tasks 精确分发到对应的基础设施层级。
    - *示例：Task 1 (L1): 扩容磁盘；Task 2 (L3): 迁移数据。*

### 3. Actions (具体执行步骤)
- **原子操作**：为每一个 Task 制定具体的 Action 序列。
- **SSOT 对齐**：Actions 必须符合 [**Ops Standards**](./docs/ssot/ops.standards.md) 的防御性守则。
- **闭环变更**：Actions 必须包含：修改代码 -> 更新 SSOT -> 验证生效 。

### 4. Result (结果验证)
- **完工自检**：对照 `docs/project/` 中的 Project 进行检查。未完成则记录 todo，完成则将 Project 移动到 `docs/project/archived/`。
- **证据闭环**：通过相关 SSOT 文档 "The Proof" 章节定义的测试来证明结果。
- **更新 README**：更新文件所在目录的 README.md，Project 文档，SSOT 文档。

---

# 🚨 核心强制原则 (SSOT First)

1.  **SSOT 为最高真理**：基础设施的 **唯一权威来源** 是 `docs/ssot/`。README 仅作为导航。
2.  **无 SSOT 不开工**：引入新组件前，必须先在 `docs/ssot/` 定义其真理（架构、约束、SOP）。
3.  **禁止隐性漂移**：发现代码与 SSOT 不符时，必须立即同步修正，严禁让 SSOT 腐烂。

---

## 文档准则
### 文档归类

本平台的文档分为四类，各有侧重：一般来说，记录顺序为：Project -> Layer README -> SSOT -> 开发者体验。逐步要求更加严格和精炼准确。

| 分类 | 路径 | 用途 | 适合人群 |
|------|------|------|---------|
| **[Project](./docs/project/)** | `docs/project/` | 项目追踪，任务管理 | AI / 维护者 |
| **Layer README** | 各目录 `README.md` | 目录介绍，设计和维护指南 | 基础设施维护者 |
| **[SSOT](./docs/ssot/)** | `docs/ssot/` | 复杂话题集中管理，技术参考手册 | 所有人 |
| **[开发者体验](./docs/onboarding/)** | `docs/onboarding/` | 场景驱动，注重接入顺滑 | 应用开发者 |

> **README 模板**：参考 [docs/README_tempate.md](./docs/README_tempate.md)

### AI 文档行为约束

1. **禁止随意生成文件**：需要记录的内容应集中放入对应 `Infra-XXX.TODOWRITE.md`（同编号）。
2. **Project 文件配对**：每个 Project 必须包含两份文件：`Infra-XXX.<project>.md` 与 `Infra-XXX.TODOWRITE.md`（同编号）。归档后合并为单文件（见 `docs/project/README.md`）。
3. **Project 目录规则**：
   - `Infra-001.bootstrap_and_setup.md` **未经授权只可以加东西不可以删东西**。
   - 你的 artifacts 简要记录到对应 `Infra-XXX.TODOWRITE.md`，不可随意创建新 Project 文件。
4. **每次修改必须更新**：
   - 修改代码 → 更新对应目录的 `README.md`
   - 涉及架构变更 → 更新相关 SSOT 文档

> **现行项目编号注记（以 `docs/project/README.md` 为准）**  
> 若本节规则与当前项目结构不一致，请优先遵循 `docs/project/README.md`，并在执行完毕后同步更新本文件。

### 知识库导航 (The Truth)

👉 **[SSOT Documentation Index (docs/ssot/README.md)](./docs/ssot/README.md)**

| 查阅内容 | 对应 SSOT 文件 / 章节 |
|----------|----------------------|
| **防御性运维/守则** | [**Ops Standards / Defensive Maintenance**](./docs/ssot/ops.standards.md#3-防御性运维守则-defensive-maintenance) |
| **Provider 优先级** | [**Ops Standards / Provider Priority**](./docs/ssot/ops.standards.md#2-托管资源评估-sop-provider-priority) |
| **密钥流转/契约** | [**Bootstrap Vars & Secrets SSOT**](./docs/ssot/bootstrap.vars_and_secrets.md) |
| **故障恢复 SOP** | [**Recovery SSOT**](./docs/ssot/ops.recovery.md) |
| **流水线操作** | [**Pipeline SSOT**](./docs/ssot/ops.pipeline.md) |

## 代码准则

### 代码风格

- **DRY**：避免重复代码，使用函数/类/模块/组件等抽象。
- **避免魔法数字**：使用常量/枚举/配置文件等替代。
- **尽可能复用已有的库**：动手前永远先检查 libs 目录 [README.md](./libs/README.md)。

### PR提交准则

- **checklist**: 要在 description 里面列checklist。
- **检查 wiki 完备程度**：包括 SSOT、Project、README 等。
- **推送前检查**：确保没有冲突。已有的 Code Review评论都已经处理（resolved）。

### 线上测试

- **测试分支**：你可以先推送分支，然后新建一个 staging 环境，然后使用你的分支做测试。
- **测试过程**：请你假设你自己就是用户，会从 web / cli / ssh 等方式来确保你的变更符合预期。
- **保持稳定**：代码提交后，必须保证staging环境符合预期了，我们才去合并 PR。

### 运营准则

- **工具优先级**：mcp > cli > api > ssh > web浏览器。
- **drift修复**：可以手动创建测试或者修补线上问题。但是要确保代码修复，同时合并之后要 apply 一次消除 drift。
- **0宕机原则**：如果有宕机风险，必须主动提出问题。如果必须宕机，必须提出方案，降低宕机时长。


---

# 安全与红线
- **严禁** 提交任何敏感文件 (`*.pem`, `.env`, `*.tfvars`)。
- **状态不一致处理**：Apply 冲突时必须执行 [**State Discrepancy Protocol**](./docs/ssot/ops.standards.md#rule-4-状态不一致协议-state-discrepancy-protocol)。
- **密钥源头**：1Password 是静态密钥的唯一真源。
