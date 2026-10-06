---
name: start
description: 工作流启动入口。开工前检视存量武器库、检索历史记忆、认领 Issue 并创建隔离 Worktree（已被 /init 全面纳管）。
---

# 🚀 /start — 工作流入口

> **状态指引**：本 Skill 的开工盘点与工作区隔离能力已被 **`/init`** 完整纳管。直接执行 `/init` 即可。

---

## 核心开工判据

1. **存量优先 (Inventory First)**：开工前必须先查看 `tools/` 与 `libs/`，严禁盲目手写重复轮子。
2. **独占 Worktree 检查与创建**：必须在独立 Worktree（`<repo>_issue<N>_<slug>`）中开发，禁止在共享主目录修改。
   ```bash
   # 检查目标 issue 是否已被占领
   git worktree list | grep -F "_issue<N>_"

   # 创建独立 Worktree
   git worktree add ../<repo>_issue<N>_<slug> -b feat/issue<N>-<slug>
   ```
3. **断点继承**：开工前必须检查是否有未结的 handover 记忆或 Issue 断点。
