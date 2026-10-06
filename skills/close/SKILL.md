---
name: close
description: 会话收尾与生命周期检查清单。核验代码入库状态、判定 Complete/Suspend 模式、沉淀 handover 断点、执行 Prod Gatekeeper 哨兵请示并清理现场。
---

# 🏁 /close — 会话收尾与生命周期门禁

本 Skill 是智能体工作流的统一退出点。负责验证交付完整性、记录断点、阻断未授权生产上线并安全清理工作区。

> **核心军规：未合主干禁称完成，生产上线严禁私断。**
> PR 未合入 main 分支时，严禁进入 Complete 模式。服务变更未获 Owner 明确授权前，严禁部署生产。

---

## 阶段一：收尾模式判定

在执行任何退出动作前，通过物理脚本判定当前状态：

```bash
BRANCH="$(git branch --show-current)"
PR_STATE=$(gh pr view "$BRANCH" --json state -q .state 2>/dev/null || echo "NONE")
```

| 条件 | 模式 | 行为要求 |
|---|---|---|
| 分支未合入 `main` (`PR_STATE != "MERGED"`) | **Suspend 模式** | 严禁关闭 Issue，严禁删除 Worktree，沉淀 Handover 记录。 |
| PR 已合入 `main`，且**有服务或底层配置变更** | **Prod Gatekeeper 判定** | 未获 Owner 授权时禁止退出，必须展示三阶段状态并请示。 |
| PR 已合入 `main`，且**无服务影响**（纯文档/测试）| **Complete 模式** | 执行完整收尾检查清单，清理 Worktree，关闭/更新 Issue。 |

---

## 阶段二：Suspend 模式 — Handover 沉淀

当任务中断、挂起或等待合流时，将现场精准沉淀进记忆库与 Issue：

```bash
# 写入持久记忆库
ws-bm write-note --folder memory/handover --tags handover --title "Handover: issue-<N>" --content "..."
```

必须按标准格式记录四要素：
1. **做了什么 (Done)**：已提交的 Commit SHA、改动的核心文件与验证通过的单测。
2. **卡在哪 (Blocker)**：当前阻塞原因（CI 异常、未决设计冲突、等待审批）。
3. **关键决策 (Decisions)**：本次会话锁定的架构契约与被推翻的旧假设。
4. **下一步行动 (Next)**：下次会话 `/init` 后应该执行的第一条物理命令。

---

## 阶段三：Prod Gatekeeper 生产哨兵（服务变更必检）

若改动涉及服务容器、Docker Compose、基础设施或底层配置：

1. **三阶段状态表格**：打出三阶段真实事实汇报给 Owner：
   ```text
   [PROD GATEKEEPER CHECKPOINT]
   - Stage 1 (Merge): Commit SHA <sha>
   - Stage 2 (Staging Soak): Run URL <url>, Soak Duration <duration>, Health: PASS
   - Stage 3 (Prod Baseline): Image <digest>, Watchdog: OK
   
   请示：是否授权部署 Prod？（回复 "deploy" 授权部署，或回复 "hold" 暂不上线）
   ```
2. **fail-closed 闭环**：未收到 Owner 明确回复前，降级为 Suspend 模式，出让控制权等待响应。

---

## 阶段四：Complete 模式 — 现场清理 (Teardown)

所有交付条件与核验均通过后，清理物理环境：

1. **终止后台残留**：检查并杀死当前 Worktree 下遗留的测试进程、Watcher 或马仔。
2. **移除独立 Worktree**：
   ```bash
   git worktree remove ../<repo>_issue<N>_<slug>
   ```
3. **关闭或同步 Issue**：在 GitHub Issue 中附上合入证明与关闭说明。
