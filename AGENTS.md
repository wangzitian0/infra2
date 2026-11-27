# Infra2 Harness and Infrastructure AI Agent Rules

> **Authority boundary:** This repository owns its own `AGENTS.md` directly. The agent may merge a PR when every merge gate passes (standing authority is a Workspace-tier fact; this repository's criteria are in [`docs/ssot/ops.merge-gate.md`](docs/ssot/ops.merge-gate.md)). If any required state fails, is missing, or cannot be verified, fail closed and do not merge.

> **Keep only decision invariants here.** Load procedures on demand through the links below. Long always-loaded instructions dilute hard boundaries.

## Production reservation

The single definition of the production reservation. Other tiers, skills and gates point here.

- **Only a production change returns to the owner.** A production change alters what production runs or stores (deploy, promote or apply to production; production secret write; manual production data write or deletion; production DNS, route or host change; unrecallable release; or scheduled unapproved production code). Each Repo tier names its production paths. A path can be a merge or a release-checkout pull.
- **Approval:** the owner approves the exact commit SHA or release tag of each production change, and is present when it runs. A "deploy" reply covers only the release that it names. Immediately before a production dispatch, get a live reply from the owner.
- **Self-guard:** an edit that removes, narrows or redefines the production reservation needs the owner's approval of its head SHA. This includes an edit that makes fewer production runs wait for the owner (deploy-path approval check, production job classification, or production environment settings). It also covers removing or weakening a pin of this section, its pin test, or the required check that runs the test.
- **Everything else is the agent's:** in any repository the owner owns, every non-production change (including `dev_env` outside the self-guard), staging and canary deploy, release tag, rule text, skill, gate/release code, and required-check edit. Before merging gate or release code, an independent read-only reviewer inspects the head SHA and states whether the gate gets stricter, equal or looser. A looser edit in the self-guard follows the self-guard.
- **Every other hold is a defect:** a rule, skill, label or gate holding non-production work for the owner is a defect. Do the work, then fix the hold. `ws_owner_scope` fails on it.

## Harness scope and precedence

This repository is both the implementation/deployment control plane for `infra2` and a multi-repository development workspace. The machine inventory is [`harness/repos.yaml`](harness/repos.yaml); architecture boundaries are in [`docs/ssot/core.harness.md`](docs/ssot/core.harness.md).

1. **Harness focus:** This file directly governs `infra2` work at the root. Workspace emphasis covers `infra2`, `infra2-sdk`, and general collaboration preferences.
2. **App autonomy:** `repos/finance_report` and `repos/truealpha` are workspace checkouts. On entering an App, read its own `AGENTS.md` and architecture documents; the App's local rules take precedence. Each App owns its own `AGENTS.md`; the harness does not define or overwrite App policy. **Merge authority is not App policy:** it is a standing owner grant across their repositories and does not need to be requested again on changing repositories.
3. **Preferences are not cross-repository commands:** Default GitHub, collaboration, and design preferences live in [`harness/workspace/`](harness/workspace/). More specific rules in the target repository prevail.
4. **Dependency boundary:** A submodule is a development snapshot, not a package, runtime, deployment, or configuration-hash dependency. Stable cross-repository code contracts travel only through a published `infra2-sdk` version.
5. **Submodule development boundary:** Submodule checkouts in `repos/` must remain detached or on tracked commits matching parent pins. Develop submodule changes in independent checkouts or worktrees outside the harness working tree.
6. **Canary role:** This repository is the first consumer and Canary proving ground for released `infra2-sdk` versions before downstream adoption. The Canary is the multi-slot Canary Todo service (`platform/30.todo`, `slots.yaml` — `v4-13`, `v4-14`). Its SSOT is [`docs/ssot/platform.canary_todo.md`](docs/ssot/platform.canary_todo.md). Probes read each slot's `/api/canary/status` every 60 s to prove Redis writes, Postgres logins, and S3 writes.

## Core mandatory principles (SSOT first)

1. **SSOT is authoritative:** [`docs/ssot/`](docs/ssot/README.md) is the only authority for infrastructure facts.
2. **Define truth before implementation:** Specify a new component's architecture, constraints, and SOP in `docs/ssot/` before implementing it.
3. **No hidden drift:** When code and SSOT disagree, correct the mismatch immediately; do not let the SSOT decay.
4. **Key List Protocol:** Check `docs/ssot/MANIFEST.yaml` first; reuse existing components or register new keys in the same PR (`init` skill §1).

## Merge gate (minimum decision criteria)

Full procedures are in [`docs/ssot/ops.merge-gate.md`](docs/ssot/ops.merge-gate.md). **Any unmet criterion below fails closed and prohibits merge:**

1. **Same head:** Checks, review, and merge must refer to the same `head SHA`. Old local results or stale reviews are not substitutes.
2. **Merge Authority green:** Every applicable check marked `blocks_merge: true` in [`docs/ssot/ci-gate-inventory.yaml`](docs/ssot/ci-gate-inventory.yaml) must conclude success. Pending, failed, cancelled, unexpectedly skipped, or unreadable counts as unmet.
3. **Review closure:** Weight unresolved findings by literal severity: high=1.0, middle=0.5, low=0.25, and unlabeled=middle. A total of at least 1.0 blocks merge. Let `pr_merge_gate` calculate it; do not estimate manually. Only a literal `severity: <level>` annotation defines severity; prose tone does not.
4. **Green must be current and actually reported:** Use `python -m tools.pr_merge_gate <n> --policy either --merge` rather than reading checks visually (Copilot review is optional and non-blocking). Exit codes: 0 ready (`--merge` runs); 1 wait; 2 production path (owner approval required); 3 review set (`review_required`) or action required; 4 could not evaluate (pull main or retry). When `review_required` is the only exit-3 reason, record an independent review of the head SHA and re-run `tools.pr_merge_gate`. A production path of rule 6 stops the merge whatever the exit code. Exit 2 or 3 for a non-main base means retarget the PR. Release code here is `tools/reconcile_iac_inputs.py`, `tools/deploy_v2.py`, `tools/promotion_soak_guard.py`, and their imports.
5. **Production reservation:** the Root section of that name is the only definition of what the owner decides. This repository also names as production: L1 bootstrap self-update, a runner rebuild (the `bootstrap` job of `deploy.yml`, whatever its `type` input), and the observability apply. `tools/pr_merge_gate.self_governing_files()` computes the gate's closure (`ops.merge-gate.md`).

   **Release train skill:** `release-train` owns Staging batching and the open production decision scan.

   **Three-stage lifecycle -- merge != deploy (owner instruction, 2026-09-25):**
   - **Stage 1 -- Code Merge:** `git merge` lands commits on `main` (zero service impact).
   - **Stage 2 -- Staging Deploy:** Triggered by pushing tag `vX.Y.Z` (auto-deploys staging soak only).
   - **Stage 3 -- Production Deploy:** Requires explicit dispatch (`reconcile-iac-inputs.yml promote_prod=true`) after [`deploy-evidence`](.agents/skills/deploy-evidence/SKILL.md) and the `close` skill.
6. **Review set and production paths** (owner instructions, 2026-09-22, 2026-10-06, 2026-10-08 and 2026-10-20). The gate verifies the GitHub `production` environment lock and `common/gate/production_contract.py` on every open PR, printing `production lock verified` (`lock_state=verified`) when satisfied (`ops.merge-gate.md`). The review set is the closure of rule 5 and the contract tables; edits in the review set are the agent's after an independent review of the head SHA (clearing exit 3 `review_required`) and a clean main gate checkout. These edits are production changes in this repository, whatever the exit code. First, an edit to the settings of the `production` environment. Second, an edit to the jobs `host-hygiene-schedule` or `facet-reconcile` of `ops-checks.yml`, or to `tools/host_hygiene_schedule.py` or `tools/facet_reconcile.py`. Third, an edit to a workflow or to the release code when the verdict does not print `production lock verified`. Without the lock, such an edit can reach production with no reviewer. An edit that makes the contract or the lock facts gate fewer production jobs follows the Root self-guard. `common/gate/self_governance.py` owns `_direction_proof_for()`; do not copy its list.

## Security and hard boundaries

- Never commit sensitive files (`*.pem`, `.env`, `*.tfvars`).
- On an infrastructure Apply conflict, follow the [State Discrepancy Protocol](docs/ssot/ops.standards.md#rule-4-状态不一致协议-state-discrepancy-protocol).
- 1Password is the only authority for static secrets.
- Surface any downtime risk. If downtime is unavoidable, provide a plan to shorten it.

## Load on demand

[`docs/README.md`](docs/README.md) is the single navigation index; do not copy its directory tree here.

| Task | Read |
|---|---|
| Read architectural charter, Canary SDK role, and IaC layer boundaries | [`init.md`](init.md) |
| Overall engineering overview and quick start | [`README.md`](README.md) |
| Open a PR, decide merge readiness, or merge | [`docs/ssot/ops.merge-gate.md`](docs/ssot/ops.merge-gate.md) |
| Write code/docs, split tasks with STAR, or apply operating principles | [`docs/ssot/core.engineering.md`](docs/ssot/core.engineering.md) |
| Find technical truth, architecture, or SOPs | [`docs/ssot/README.md`](docs/ssot/README.md), generated from `MANIFEST.yaml` |
| Batch the Staging train, or read open production decisions | [`release-train`](.agents/skills/release-train/SKILL.md) |
| Find the current task | [`docs/project/README.md`](docs/project/README.md) |
| Integrate an App or onboard | [`docs/onboarding/README.md`](docs/onboarding/README.md) |
| Change an infrastructure layer | Its README: [bootstrap](bootstrap/README.md), [platform](platform/README.md), [tools](tools/README.md), or [common](common/README.md) |
