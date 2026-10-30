<!-- WS_STATIC_START adapter=rules-v2 inputs=b484c30c4dee1bc4b73a8ded9bbd24bdabffa7b6624c5e33491fd28d2ff4ae83 -->
<!-- Generated file: do not edit by hand. These rules are maintained in the owner's rule source and re-rendered here. -->

## Engineering discipline

- **Measure the physical system first.** Inspect the system with read-only probes (`time cmd`, process chains, file locks) before proposing architecture. Never combine validation and mutation in one command. Measure, read the result, then decide.
- **Green does not prove truth.** The tested system writes its own unit tests, CI, and issue states. Cross-check critical conclusions against two external sources not written by this repository.
- **Tests must be falsifiable.** Never hide assertions inside `if` branches or accept tautologies. Source-text `indexOf` matches against prose are not integration tests. Avoid duplicate test function names that shadow earlier tests. Make every new test fail under a relevant mutation; fake tests are CRITICAL merge blockers.
- **Falsifiable documentation and assertions:** State test requirements, error messages, and specifications in concrete ASD-STE100 terms without vague qualifiers.
- **One source of truth:** Keep one authoritative definition per core fact. Repeated hardcoding and scattered configuration invite drift.
- **Clean up during migration:** After the new mechanism is live and equivalence is proved, remove its predecessor, obsolete files, and dead code in the same change. Define contracts first and derive CI from them.
- **Deletion can leave guards green and empty:** A guard for an old structure can stop checking anything after deletion. Check each guard and remove it or redirect it to the new structure; green tests alone do not prove safe deletion.
- **Define guard scope from what it must govern, not from today's passing tree.** Let the guard fail on existing violations, then repair them. A guard never seen failing is not yet evidence of protection.
- **Worktree self-sufficiency:** Every worktree must resolve its dependencies, toolchain, skills, and configuration internally. Tools and tests must not navigate upward with `../..` to locate files in parent checkouts.
- **Physical context verification:** Static file existence does not prove host runtime context injection. Junior runs non-interactive probes (`-p` or headless exec) with low-cost models, asserting visible specification text across root and deep subdirectories.
- **Host verification scope:** Official host CLIs are the primary execution environment. Regular testing covers Antigravity 2.0 project workspaces (`.agents/skills/` and project rules); standalone desktop chat apps without directory awareness are excluded.
- **Local and remote test segregation:** Inner-loop verification (Senior, Junior, Intern) runs only focused tests on changed files (`pytest <file>::<test> -x`). Full-suite regression runs on remote CI, or locally at most once before merge.
- **Bounded file reads:** Pass `StartLine` and `EndLine` (`<= 800` lines per call) when reading existing files with `read_file` or `view_file`.

## Delivery and merge

- **ASD-STE100 specification for all artifacts:** Write committed artifacts, rules, code, comments, commits, and docs in ASD-STE100 English. Keep instructions `<= 20` words and descriptions `<= 25` words in active voice without vague qualifiers. Operational skills (`skills/**/SKILL.md`) follow the target repository's language policy.
- **Deliverable format:** Deliver a mergeable PR or a traceable issue, rather than a process-only report. Use issues and PRs as the collaboration bus. Search for an existing similar issue before creating one; update it if it exists.
- **Fail fast left to right:** Put the cheapest and likeliest failure checks first.
- **Review standing authorization:** Resolve a review thread after verifying in source code (not redacted diffs) that it is fixed or obsolete. Never resolve actionable or unverified feedback. Turn false reports into falsifiable invariant tests.
- **Weighted review gates:** Each repository defines its own severity weights and blocking thresholds. Read literal `severity: <level>` tags; do not infer severity from prose.
- **Merge when ready:** Once all merge conditions pass, merge and continue from the latest main rather than piling up divergent branches.
- **Delivery state invariant:** Never declare complete, done, or finished before merge to main and a verified Business Reality Probe (`ws-delivery-status`). State labels are `'Draft'`, `'Local verified'`, `'In review'`, `'Stage 1 Landed'`, and `'VERIFIED_COMPLETE'`.
- **Anti-exclusion and reality invariant:** Exclusion is not resolution. Never exclude failing entities or add exemptions to manufacture green checks. A task is complete only when a physical reality probe confirms non-empty execution.
- **Three-stage deployment prerequisite:** Stage 1 (Merge to main) is a non-bypassable physical prerequisite for Stage 2 and Stage 3. If HEAD is not on main, fail closed and refuse any deployment request.
- **Deploy provenance invariant:** Before dispatching any deploy workflow, physically verify that the target ref or tag contains current branch commits (`git merge-base --is-ancestor HEAD <target>`). Never deploy an older tag as completion evidence for current work.

## Production reservation

The single definition of the production reservation. Other tiers, skills and gates point here.

- **Only a production change returns to the owner.** A production change alters what production runs or stores. Examples: a deploy, promote or apply to production; a production secret write; a manual production data write or deletion. Also a production DNS, route or host change, and a release that cannot be recalled. Code that reaches production with no later owner approval, for example on a schedule, counts too. Each Repo tier names its production paths.
- **Approval:** the owner approves the exact commit SHA or release tag of each production change, and is present when it runs. A "deploy" reply covers only the release that it names. Immediately before a production dispatch, get a live reply from the owner.
- **Self-guard:** an edit that removes, narrows or redefines the production reservation needs the owner's approval of its head SHA. This includes an edit that makes fewer production runs wait for the owner. Examples: the owner-approval check of a deploy path, a production job classification, the settings of a production environment. It also includes an edit that removes or weakens a pin of this section or its dev_env pin test. The same holds for the required check that runs the test.
- **Everything else is the agent's:** in a repository that the owner owns, every merge, staging and canary deploy, and release tag. Also rule text, skills, gate code, data and tests, required-check workflows and release code. Before you merge gate or release code, a read-only reviewer that did not write it checks the head. The review states whether the gate gets stricter, stays equal or gets looser. A looser edit in the self-guard list follows the self-guard.
- **Every other hold is a defect:** a rule, skill, label or gate exit code that holds non-production work for the owner. Do the work, then fix the hold. The dev_env check `ws_owner_scope` fails on it.

## Runtime safety

- **Reason from the worst case.** For environment changes, wrappers, redirection, or interception rules, check for no-TTY deadlock in CI or child processes, concurrent shared-file truncation/races, and network or cold-start failure cascades. Reject a proposal whose lack of backlash cannot be established.
- **Treat three hidden green failures as defects:** WRONG FORMULA (an incorrect formula passes assertions), GREEN-WHILE-EMPTY (filtering removes all output but reports success), and STALE-REPORTED-AS-FRESH (old data is labeled fresh). Implausible output is evidence of a defect.
- **Protect ambient services.** Default unit tests and Junior tasks must not destructively act on host ports, shared background processes, or development databases (`DROP`, `TRUNCATE`, `--clear`, forced restart). Resets require an isolated sandbox/worktree with a dedicated random port, or an explicit `CI=true` or `ALLOW_CLEAR_TEST=1` guard; otherwise skip safely with a warning.
- **Two triggers:** Every background job or batch process needs both scheduled execution and manual replay.
- **Do not steal CD locks:** A trigger is instant but publication is delayed. Do not interrupt a running deployment; the next run must coalesce commits accumulated while it was busy.

# Infra2 Harness and Infrastructure AI Agent Rules

> **Authority boundary:** An AI agent changes this file through its dev_env source and the dev_env rule-text checklist. The agent may merge a PR when every merge gate passes (standing authority is a Workspace-tier fact; this repository's criteria are in [`docs/ssot/ops.merge-gate.md`](docs/ssot/ops.merge-gate.md)). If any required state fails, is missing, or cannot be verified, fail closed and do not merge.

> **Keep only decision invariants here.** Load procedures on demand through the links below. Long always-loaded instructions dilute hard boundaries.

## Harness scope and precedence

This repository is both the implementation/deployment control plane for `infra2` and a multi-repository development workspace. The machine inventory is [`harness/repos.yaml`](harness/repos.yaml); architecture boundaries are in [`docs/ssot/core.harness.md`](docs/ssot/core.harness.md).

1. **Harness focus:** This file directly governs `infra2` work at the root. Workspace emphasis covers `infra2`, `infra2-sdk`, and general collaboration preferences.
2. **App autonomy:** `repos/finance_report` and `repos/truealpha` are workspace checkouts. On entering an App, read its own `AGENTS.md` and architecture documents; the App's local rules take precedence. Each App's Repo tier is projected from its own dev_env source; the harness does not define or overwrite App policy. **Merge authority is not App policy:** it is a standing owner grant across their repositories and does not need to be requested again on changing repositories.
3. **Workspace tooling:** Root-level `oh-my-code-agent/` is an independent submodule for TUI management. It evolves independently and must not become a runtime or source dependency of infra or Apps.
4. **Preferences are not cross-repository commands:** Default GitHub, collaboration, and design preferences live in [`harness/workspace/`](harness/workspace/). More specific rules in the target repository prevail.
5. **Dependency boundary:** A submodule is a development snapshot, not a package, runtime, deployment, or configuration-hash dependency. Stable cross-repository code contracts travel only through a published `infra2-sdk` version.
6. **Submodule development boundary:** Submodule checkouts in `repos/` and `oh-my-code-agent/` must remain detached or on tracked commits matching parent pins. Develop submodule changes in independent checkouts or worktrees outside the harness working tree.
7. **Canary role:** This repository is the first consumer and Canary proving ground for released `infra2-sdk` versions before downstream adoption. Canary deployments (`tools/deploy_v2_canary.py`) are ephemeral on-demand sandboxes for immediate route and alert verification. Canary is not a persistent background daemon or recurring cron job. Do not schedule continuous background runs for Canary.

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
4. **Green must be current and actually reported:** Use `python -m tools.pr_merge_gate <n> --policy either --request-review --merge` rather than reading checks visually. Exit codes: 0 ready (`--merge` executes autonomously), 1 wait, 2 review set, 3 action required, 4 could not evaluate (pull main or retry). When the review set of rule 6 is the only exit-2 reason, get the independent review and merge with `gh pr merge --squash --match-head-commit <sha>`. A production path of rule 6 stops the merge whatever the exit code. Exit 2 for a base other than main means retarget the PR. Release code here is `tools/reconcile_iac_inputs.py`, `tools/deploy_v2.py`, `tools/promotion_soak_guard.py`, and their imports.
5. **Production reservation:** the Root section of that name is the only definition of what the owner decides. This repository also names as production: L1 bootstrap self-update, a runner rebuild (the `bootstrap` job of `deploy.yml`, whatever its `type` input), and the observability apply. `tools/pr_merge_gate.self_governing_files()` computes the gate's closure (`ops.merge-gate.md`).

   **Three-stage lifecycle -- merge != deploy (owner instruction, 2026-09-25):**
   - **Stage 1 -- Code Merge:** `git merge` lands commits on `main` (zero service impact).
   - **Stage 2 -- Staging Deploy:** Triggered by pushing tag `vX.Y.Z` (auto-deploys staging soak only).
   - **Stage 3 -- Production Deploy:** Requires explicit dispatch (`deploy.yml type=prod` or `reconcile-iac-inputs.yml promote_prod=true`) after [`deploy-evidence`](.agents/skills/deploy-evidence/SKILL.md) and [`close`](.agents/skills/close/SKILL.md).
6. **Review set and production paths** (owner instructions, 2026-09-22, 2026-10-06, 2026-10-08 and 2026-10-20). The gate verifies the GitHub `production` environment lock and `common/gate/production_contract.py` on every open PR, printing `production lock verified` (`lock_state=verified`) when satisfied (`ops.merge-gate.md`). The review set is the closure of rule 5, the contract tables, and the release code; edits in the review set are the agent's after an independent review, `### Mutation evidence` naming tests, and a clean main gate checkout. These edits are production changes in this repository, whatever the exit code. First, an edit to the settings of the `production` environment. Second, an edit to the jobs `host-hygiene-schedule` or `facet-reconcile` of `ops-checks.yml`, or to `tools/host_hygiene_schedule.py` or `tools/facet_reconcile.py`. Third, an edit to a workflow or to the release code when the verdict does not print `production lock verified`. Without the lock, such an edit can reach production with no reviewer. An edit that makes the contract or the lock facts gate fewer production jobs follows the Root self-guard. `common/gate/self_governance.py` owns `_direction_proof_for()`; do not copy its list.

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
| Find the current task | [`docs/project/README.md`](docs/project/README.md) |
| Integrate an App or onboard | [`docs/onboarding/README.md`](docs/onboarding/README.md) |
| Change an infrastructure layer | Its README: [bootstrap](bootstrap/README.md), [platform](platform/README.md), [tools](tools/README.md), or [common](common/README.md) |
<!-- WS_STATIC_END -->
