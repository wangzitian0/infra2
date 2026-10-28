# Infra2 合流门禁

> [AGENTS.md](../../AGENTS.md) 只保留判定所需的最小充分条件与 fail-closed 原则。
> 本文是逐条细则与其理由。任一状态失败、缺失或无法验证时必须 fail-closed，禁止合流。

## PR 提交准则

- **checklist**：要在 description 里面列 checklist。
- **检查 wiki 完备程度**：包括 SSOT、Project、README 等。
- **推送前检查**：确保没有冲突。已有的 Code Review 评论都已经处理（resolved）。

## 可合流条件（AI Merge，全部必需）

- **常设合流权**是 workspace 级事实（工作区根 `AGENTS.md`「合流授权」），本文不重述其范围；
  本文只定义 infra2 自己的门禁：标准 PR 流程走完、下列**客观可判定**条件全部满足的 PR，AI
  可自行合流，剩下仍需 owner 的两类变更见文末。
- **合流真源唯一**：目标分支正确，PR `mergeable`，无冲突；检查与合流必须针对同一个 `head SHA`，禁止用本地旧结果或旧 review 代替。
- **Merge Authority 全绿**：[`docs/ssot/ci-gate-inventory.yaml`](ci-gate-inventory.yaml) 中该变更适用且 `blocks_merge: true` 的检查全部成功；pending、failure、cancelled、意外 skipped 或无法读取均视为不满足。
- **Review 已闭环（加权阻塞）**：required review 已满足，所有 actionable conversation / review threads 已处理并 resolved；不得自行忽略、dismiss 或用过期 review 代替当前 head 审查。未 resolved 的 review 发现（不论来源——human reviewer、Copilot、/code-review 等）按 severity 加权计分：high=1.0、middle=0.5、low=0.25，未标注 severity 的按 middle 计。**未 resolved 发现的加权总分 ≥ 1.0 即视为未闭环、禁止合流**，不要求单条 high 才阻塞——例如 2 条 middle 或 4 条 low 累计到位同样阻塞。达到门槛后必须逐条修复，或取得 owner 对具体发现的明确豁免并留痕，方可标记为已处理。
- **绿必须是当前的**：检查只证明它跑过的那棵树。兄弟 PR 合入后改写了本 PR 也动的文件时，
  所有检查**仍然是绿的**——因为没有一个重跑过。因此必须比较 `base...head`，交集非空即更新分支重跑。
- **必需检查必须在场**：`blocks_merge: true` 的检查不仅要绿，还必须**确实报告过**。
  `detect-changes` 失败时其下游 job 是 skipped 而非 run，GitHub 仍接受为已满足——
  因此必需集须以 `ci-gate-inventory.yaml` 为准逐个核对在场与结论，而不是只看已报告的检查是否为绿。
- **静置 (settle)**：An automated review (Copilot) of the **current head SHA** starts a settle time of 60 seconds.
  Copilot does not review a pull request that an app or bot account opens (#1075).
  For such a pull request, an independent verification comment replaces the review.
  Its heading is `### Independent verification of head <sha7>`, where `<sha7>` is the first 7 characters of the current head SHA.
  A later line of the comment names at least one test, as `test_...` or `path::name`.
  The settle time starts at the creation of the comment, or at its last edit if it has one. The newest matching comment counts.
  For a pull request that a user opens, a comment does not replace the review.
  The limit is always 12 minutes after the last push. Without a review or a verification comment, that limit applies.
  The earlier of the two times decides. A fix-up push does not start a new Copilot review. Request it explicitly.
- **变更契约完整**：PR description checklist 完整；代码、测试、SSOT、Project、Layer README / Onboarding 按影响同步；无未解释的 scope drift；PR description 显式引用其推进/关闭的 issue 编号（无则写明 None）——避免 PR 实质推进了某 issue 的 scope 却不留痕迹，导致 issue 可见状态滞后仓库实际进度（#508）。
- **安全与运维门禁**：无敏感文件；已说明风险、回滚与 0 宕机影响；涉及 state discrepancy、密钥或生产数据时已按对应 SSOT 执行并留证。
**仍需 owner 的两类，判据各不相同，分别列**：

- **First class: the target environment decides. Staging is the agent's; production is the owner's** (owner approval, 2026-09-21).
  The agent merges a change that starts a staging deploy, the canary on the reserved slot `pr-0`, or the `report-branch-main` preview.
  The canary is the `deploy-v2-canary` job of `ops-checks.yml`. These merges need no approval.
  These production jobs need the owner: prod apply, prod promote, L1 bootstrap self-update, and the observability apply.
  The runner rebuild that `bootstrap/06.iac_runner/**` starts also needs the owner.
  When the production lock holds, a merge that starts one of these jobs only creates a run.
  That run waits until the owner approves it in the `production` environment. The merge itself needs no approval.
  When the lock does not hold or cannot be read, the merge needs the owner's approval of the current head SHA.
  An approval of an older head does not apply. See [Production lock](#production-lock).
  The target environment decides first, then reversibility. When the two disagree, the environment decides.
- **其二，改动"决定合流的东西"（自我裁决闭包，#787 / #1040 / #1138 / #1340）**：`tools/pr_merge_gate.self_governing_files()` 从 `pr_merge_gate.py` 出发，BFS 它 import 的每个仓内模块、它读的数据文件及各自测试，并显式包含两份规则散文（`AGENTS.md` 与本文件）。门禁仅对生产路径返回 `outcome="owner"` (`owner_required=True`)；闭包内其余非生产路径在获得当前 head 的独立审查前返回 `outcome="review_required"` (`review_required=True`, `owner_required=False`)，审查记录就位后返回 `ready=True`（详见 [Production lock](#production-lock)）。
- **受保护文件**（`CLAUDE.md`，及各 App 标注为 protected 的架构文档）：修改时在 PR description 中引用授权它的 owner 指令，使授权可追溯。
- **合流后闭环**：使用仓库允许的合流方式；确认 merge commit 已落在目标分支并监看 post-merge checks。失败时立即停止 tag / promote，报告并修复，不得继续发布。
  **但 `cancelled` 在合流前与合流后不是同一个含义**：作为 Merge Authority 一律算不满足（见上）；
  **合流后**监看时，`infra-ci.yml` 设了 `concurrency: cancel-in-progress: true`，兄弟 PR 随后合入
  main 会正当地顶替在途那一轮——这种 cancelled 不是失败，不触发上面那句「立即停止 tag / promote」。
  判据是**回去读该 workflow 的 `concurrency` 配置、确认它是否被更晚的 push 顶替**，不从状态字面
  推断（2026-09-23 实测：#815 合入后 run 报 cancelled，实为 #827 数秒后合入顶替，最新主干头三条
  检查全绿）。

## Production lock

The gate reads the production lock (#1138) on every run for an open PR in this repository.
It does not read the lock for a closed PR, for another repository, or when the run stops at exit 4 first.
The verdict prints one lock line: `production lock verified (...)` or `production lock NOT verified: <failures>`.
A run that did not read the lock prints `production lock NOT verified: not read`, with the reason.
A run that stops after the lock read prints `production lock NOT verified: the run stopped before a verdict`.
Each lock line ends with `| lock_state=<state>`. The state is `verified`, `not_verified`, `not_read` or `stopped_before_verdict`.
The JSON verdict has the same facts in `lock_state`, `lock_verified` and `lock_failures`. The line alone never changes the exit code.
The lock is the GitHub environment `production`. The gate reads four facts from GitHub:

1. The environment has a required reviewer.
2. `can_admins_bypass` is false.
3. The environment accepts deployments only through custom branch and tag policies.
4. The policies are exactly branch `main` and tag `v*`.

The gate also applies the production contract to every workflow file of the head.
The contract tables are in `common/gate/production_contract.py`.
The lock holds when the four facts are true and the head satisfies the contract. Then:

- The agent merges a workflow edit, except in a workflow that defines a required check or in the unclassified scheduled jobs `host-hygiene-schedule` and `facet-reconcile` of `ops-checks.yml`.
  A workflow that defines a required check stays in the review set (`outcome="review_required"` when the lock holds), even when the direction proof calls the edit tighter.
  The workflow proof reads only push paths and job names, not job bodies.
  So it cannot show that a required job still checks the same thing.
  The gate derives these workflows from the `blocks_merge: true` gates in `ci-gate-inventory.yaml`.
  When the gate cannot read such a gate, the lock releases no workflow file.
- A deploy-triggering path does not need the owner. Each job named as production waits for the environment reviewer.

Fail closed: when a fact is false or unreadable, the earlier rules apply to every other file. The verdict names each failure.
The gate returns `outcome="owner"` (`owner_required=True`) only for a production path (#1340):

- An edit to the settings of the `production` environment.
- An edit to the jobs `host-hygiene-schedule` or `facet-reconcile` in `.github/workflows/ops-checks.yml`, or to `tools/host_hygiene_schedule.py` or `tools/facet_reconcile.py`.
- A workflow or deploy-triggering edit when the production lock is not verified.

Every other edit in the closure (`common/gate/*.py`, `tools/pr_merge_gate.py`, `common/tests/test_*.py`, `docs/ssot/ci-gate-inventory.yaml`, `AGENTS.md`, `docs/ssot/ops.merge-gate.md`, `pyproject.toml`, shadow files, and required-check workflows when the lock holds) returns `outcome="review_required"` (`review_required=True`, `owner_required=False`) until cleared by one of these exits:

- A rule-text file clears with a quoted owner instruction in the PR body or an independent verification comment on the current head when the production lock holds.
- A change to `ci-gate-inventory.yaml` clears when the direction proof shows it is tighter.
- A review-set file clears when the lock holds (`lock_state=verified`) and the current head has a recorded automated review or an independent verification comment (#1040, #1340).
- Across a mechanical rebase that leaves `HEAD^{tree}` unchanged, citing the previously approved commit SHA (`approved: <sha>`) preserves approval (#924).

A file that Python or pytest runs before gate code is also in the gate closure.
Examples: a root module such as `yaml.py`, a package that replaces a gate module, and a `conftest.py` in `common/tests`.
Bytecode, `.so` and `.pth` files, virtual environments and `sitecustomize.py` are also in the closure, at any depth.
`pyproject.toml`, `pytest.ini`, `tox.ini`, `setup.cfg` and `.coveragerc` are in the closure at the root, in `common/` and in `common/tests/` (#1147, #1035).
`tools/host_hygiene_schedule.py` and `tools/facet_reconcile.py` are in `self_governing_files()` (#1127, #1147).
The gate compares paths case-folded, because the disk of the gate host does not tell case apart.
The contract check verifies that every required-check workflow exists at the head SHA, so renaming or deleting a required-check workflow fails the production contract (#1035).

The contract is strict in three ways.
A conditional job must use the exact condition in its table entry. A condition that only contains it fails.
A job that reads any secret outside `NON_PRODUCTION_SECRETS` holds a production credential and must be in a table.
The contract reads the composed YAML nodes, so a YAML tag or escape does not hide an expression.

Threat model.
The gate defends against mistakes and process shortcuts of an honest agent and its tools.
It is not a sandbox against a PR that is built to run code on the gate host.
That boundary is the GitHub ruleset and the reviewer of the `production` environment.

Limits of the lock:

- The lock reader counts reviewers; it does not check who they are.
- One run makes at most 9 + N + 2k `gh` calls. N is the number of workflow files.
  k is the number of changed gate files with a direction proof. `--request-review` adds up to 2, and `--merge` adds 1.
  Each call can try up to 3 times.

Residual risks:

- The lock does not cover the production credentials that ungated jobs read.
- The bodies of ungated jobs remain outside the gate (#1147); `tools/host_hygiene_schedule.py` and `tools/facet_reconcile.py` are in `self_governing_files()`.
- The gate checks that a gated job declares the right environment.
  It does not check that the job body keeps using the input that selects the environment.
  Example: a body edit hard-codes `--type prod`, and the environment condition still reads `inputs.type`.
- `iac-runner` `/deploy` (`env=production`) enforces Layer 2 GitHub Actions OIDC verification (`environment == "production"`, `ref` matching `refs/heads/main` or `refs/tags/v*`) in addition to HMAC signing (#1126).

## 授权沿革

2026-09-08 引入会话级合流授权，以绕开逐-head 批准的等待；2026-09-21 owner 以
「只要是标准 PR 过的，且各类 check 都过了，你可以 merge 代码」取代两者，改为上文的常设合流权。
2026-09-22 owner 澄清授权覆盖其名下全部仓库，这条事实随之上提到 workspace 级规则（dev_env#59）；
本文只保留 infra2 自己的门禁条件。文末仍需 owner 的第二类（改动"决定合流的东西"本身）不属于
权限保留，而是自我裁决问题：门禁从工作树读规则，AI 合流自己的 PR 时读到的就是该 PR 引入的版本。
判定与合流统一走 `python -m tools.pr_merge_gate <n> --policy either --request-review --merge`
，不靠肉眼看表。Act on the exit code only (#740):

| Exit | Meaning | Next step |
|---|---|---|
| 0 | Ready (merged with `--merge`). | Continue from the latest main. |
| 1 | Wait. Time alone fixes it: a check is pending, no check has reported yet, or the head is settling. | Wait and re-run. |
| 2 | Production path (`outcome="owner"`) or review set (`outcome="review_required"`). | For `owner`, ask the owner about this head SHA. For `review_required`, record an independent review of the head (or retarget the PR when base is not `main`). |
| 3 | Action required. Waiting does not fix it: a red or cancelled check, an open review thread, a conflict, a draft, a PR that is not open, or a base that changed under green checks. | Fix the cause, push, re-run. |
| 4 | Could not evaluate. This is not a verdict on the PR. The gate checkout's rules differ from main's (#873), or `gh` failed or returned unreadable data. The preflight finds a stale checkout before it reads any PR state. | Pull main in the gate checkout, or retry after the `gh` failure. |

## 线上测试

- **Merge 与部署解耦**：普通开发 PR 以 `github_ci.merge_authority` 为合流门禁；preview / staging proof 不得冒充 Merge Authority，也不默认阻塞普通 PR。以 [`docs/ssot/ops.pipeline.md`](ops.pipeline.md) 和锁定版本 infra2-sdk 的 `infra2_sdk/data/stages.yaml`（stage 词汇）为准。
- **测试过程**：需要 runtime proof 时，请假设自己就是用户，从 web / cli / ssh 等真实入口验证，并保存证据。
- **发布与晋升**：release tag 必须来自 reviewed main；平台 tag 自动晋升 staging。staging 使用同一不可变 tag 验证并完成 soak 后，才可显式 promote prod，禁止跳过 staging。
