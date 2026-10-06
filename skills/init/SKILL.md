---
name: init
description: 工作流入口与需求基准初始化。盘点存量武器库（严禁盲造轮子）、检索历史记忆与决策、校验并认领 Issue、创建独立隔离 Worktree。
---

# 🚀 /init — 工作流初始化与基准确立

本 Skill 是智能体工作流的统一进入点。它在执行修改前质疑需求、盘点存量资产、检索记忆并建立物理隔离。

> **核心军规：存量优先，质疑前提，物理隔离。**
> 严禁在未盘点存量资产前重复编写脚本。严禁在共享主目录中直接修改代码。

---

## 触发时机

- 用户输入 `/init`、`/start`、“开始”、“接手” 或给出新任务。
- 接到新 Issue 编号要求开工。

---

## 执行步骤

### Step 1: 存量武器库检视 (Inventory First)

开工前必须先盘点既有资产，杜绝重复制造工具：

```bash
ls tools/                           # 查看已有 CLI、探针与门禁工具
ls libs/                            # 查看公共库与领域契约
```

1. **优先复用成熟工具**：
   - 部署与容器探测优先复用 `tools/infra_probe_runner.py`。
   - PR 门禁与加权验证优先复用 `tools/pr_merge_gate.py`。
   - 稳定性报告优先复用 `tools/stability_report.py`。
2. **严禁硬编码裸脚本**：
   - 禁止在 Skill 中硬编码未经测试的复杂单行脚本。
   - 检查 `docs/ssot/`，确认已注册的契约与信号定义。

---

### Step 2: 历史记忆与前人决策召回 (Recall)

在开始设计前，检索跨会话持久记忆库，避免重复踩坑：

```bash
# 检索关于该模块或相关技术点的历史踩坑与决策
ws-mem search "<keyword_or_module>"
# 查看本仓库近期黄金事实
ws-mem recent
```

1. **质疑历史假设**：
   - 区分客观物理事实与历史阶段性妥协。
   - 验证前人决策的前提条件在当前是否依然成立。
2. **扫描交接断点**：
   - 若存在未结 Issue，检索是否存在历史 handover 记录。

---

### Step 3: 工作区物理隔离与凭据预热

严格执行独立 Worktree 隔离，防止共享 index 污染：

```bash
# 1. 检查是否存在同名 Issue 目录或后台活跃进程
git worktree list | grep -F "_issue<N>_"

# 2. 创建独立 Issue 分支与 Worktree
git worktree add ../<repo>_issue<N>_<slug> -b feat/issue<N>-<slug>
```

1. **命名规范**：目录名与分支名必须包含严格的 `issue<N>` 标识。
2. **凭据隔离**：进入 Worktree 后加载 direnv，确保环境变量在 1 秒内就绪。
3. **自包含约束**：工具与单测必须在 Worktree 内部闭环运行，严禁使用 `../..` 向上越界引用。

---

### Step 4: Issue 状态认领与基线输出

向协作总线同步状态并输出初始基线：

```bash
gh issue view <N> --json title,body,state
```

- 输出开工摘要：确认接手的 Issue 目标、复用的工具链、召回的历史要点与 Worktree 物理路径。
