<!-- WS_STATIC_START adapter=rules-v2 inputs=76a6146a68f563ee9ff6ab9b8e4fd1ec4afcede0b1a0daa40ac1d295bea71326 -->
<!-- Generated file: do not edit by hand. These rules are maintained in the owner's rule source and re-rendered here. -->

## Engineering discipline

- **Measure the physical system first.** Before an abstract architecture proposal, inspect the system with read-only probes such as `time cmd`, process chains, and file-descriptor locks. A conceptually neat story without physical evidence is insufficient. A probe must not write. Do not combine validation and action in one command: a POST permission probe can create a resource, and a trial commit can leave a real commit. Measure, read the result, then decide, with a stop point between these steps.
- **Green does not prove truth.** The tested system writes its own unit tests, CI, and issue states. Cross-check critical conclusions against two external sources not written by this repository.
- **Tests must be falsifiable.** Do not hide assertions in `if (exists)` or `if (code != 0)` so failures run zero assertions. Do not accept tautologies such as `typeof null === 'object'` or `result !== undefined || true`. Source-text `indexOf` matches against prose are not integration tests. Duplicate test function identifiers can silently shadow earlier tests (Python `def` and duplicate JS function/const/export names); duplicate string titles in `test()` or `it()` instead run both. A green count is not coverage evidence. Make a new test fail under a relevant mutation. A read-only reviewer treats such fake-test patterns as CRITICAL merge blockers.
- **Falsifiable documentation and assertions:** State test requirements, error messages, and specifications with concrete terms adhering to ASD-STE100. Do not use vague qualifiers.
- **One source of truth:** Keep one authoritative definition for each core fact. Repeated hardcoding and scattered configuration invite drift.
- **Clean up during migration:** After the new mechanism is live and equivalence is proved, remove its predecessor, obsolete files, and dead code in the same change. Define contracts first and derive CI from them.
- **Deletion can leave guards green and empty:** A guard for an old structure can stop checking anything after deletion. Check each guard and remove it or redirect it to the new structure; green tests alone do not prove safe deletion.
- **Define guard scope from what it must govern, not from today's passing tree.** Let the guard fail on existing violations, then repair them. A guard never seen failing is not yet evidence of protection.
- **Worktree self-sufficiency:** Every worktree must resolve its dependencies, toolchain, skills, and configuration internally. Tools and tests must not navigate upward with `../..` to locate files in parent checkouts.

## Delivery and merge

- **ASD-STE100 specification for all artifacts:** Write all committed artifacts, rule definitions, source code, inline comments, commit messages, and documentation in English using the ASD-STE100 (Simplified Technical English) specification. Keep sentences short: maximum 20 words for instructions and maximum 25 words for descriptions. Use the active voice. Use approved technical words with one meaning per word. Express one topic per sentence. Do not use ambiguous qualifiers (such as "properly", "efficiently", or "seamlessly").
- **Language policy for rules and playbooks:** All rendered rule artifacts (`AGENTS.md`), manifests (`manifest.json`), source code, tests, and commit messages must strictly follow ASD-STE100 English. Operational skills and engineering playbooks (`skills/**/SKILL.md`) that contain interactive procedures follow the target repository or author team's language policy.
- **Deliverable format:** Deliver a mergeable PR or a traceable issue, rather than a process-only report. Use issues and PRs as the collaboration bus. Search for an existing similar issue before creating one; update it if it exists.
- **Fail fast left to right:** Put the cheapest and likeliest failure checks first.
- **Review standing authorization:** Resolve a review thread directly after independently verifying it is fixed or obsolete. Do not resolve actionable, ambiguous, or unverified feedback. Automated reviewers may read a redacted GitHub diff rather than source: GitHub can show `"Authorization": f"Bearer ******"` where source has `"Authorization": f"Bearer {token}"`. Check source before judging a report. When a report is false, turn the concern into a falsifiable invariant test rather than merely dismissing it.
- **Weighted review gates:** Each repository defines its own severity weights and blocking thresholds. Read literal `severity: <level>` tags; do not infer severity from prose.
- **Merge when ready:** Once all merge conditions pass, merge and continue from the latest main rather than piling up divergent branches.

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
7. **Canary role:** Canary deployments (`tools/deploy_v2_canary.py`) are ephemeral on-demand sandboxes for immediate route and alert verification. Canary is not a persistent background daemon or recurring cron job. Do not schedule continuous background runs for Canary.

## Core mandatory principles (SSOT first)

1. **SSOT is authoritative:** [`docs/ssot/`](docs/ssot/README.md) is the only authority for infrastructure facts.
2. **Define truth before implementation:** Specify a new component's architecture, constraints, and SOP in `docs/ssot/` before implementing it.
3. **No hidden drift:** When code and SSOT disagree, correct the mismatch immediately; do not let the SSOT decay.
4. **Key List Protocol:** Check `docs/ssot/MANIFEST.yaml` before creating new tools or scripts. Reuse existing components if present. If a new capability is necessary, implement the minimum logic and register the key in `MANIFEST.yaml` within the same PR. State reuse or registration in the first response.

## Merge gate (minimum decision criteria)

Full procedures are in [`docs/ssot/ops.merge-gate.md`](docs/ssot/ops.merge-gate.md). **Any unmet criterion below fails closed and prohibits merge:**

1. **Same head:** Checks, review, and merge must refer to the same `head SHA`. Old local results or stale reviews are not substitutes.
2. **Merge Authority green:** Every applicable check marked `blocks_merge: true` in [`docs/ssot/ci-gate-inventory.yaml`](docs/ssot/ci-gate-inventory.yaml) must conclude success. Pending, failed, cancelled, unexpectedly skipped, or unreadable counts as unmet.
3. **Review closure:** Weight unresolved findings by literal severity: high=1.0, middle=0.5, low=0.25, and unlabeled=middle. A total of at least 1.0 blocks merge. Let `pr_merge_gate` calculate it; do not estimate manually. Only a literal `severity: <level>` annotation defines severity; prose tone does not.
4. **Green must be current and actually reported:** A sibling PR can alter the same files after this PR's checks without triggering a rerun. GitHub may also accept a skipped required check. Use `python -m tools.pr_merge_gate <n> --policy either --request-review --merge` for the decision, rather than visually reading a check table. Act on the exit code: 0 ready, 1 wait, 2 owner, 3 action required, 4 could not evaluate (pull main or retry; not a verdict). Two exceptions until the production lock exists (#1035). First, exit 0 does not clear a change to the gate's closure, to a workflow file, or to the release code a workflow executes (`tools/reconcile_iac_inputs.py`, `tools/deploy_v2.py`, `tools/promotion_soak_guard.py` and their imports): it needs the owner whatever the exit code, because the workflow proof does not read job bodies and a self-written owner quote clears rule text. Second, exit 2 for a base other than main means retarget the PR; it is not an owner case. **Standing Merge Authority:** Code merge to main is fully autonomous. When `pr_merge_gate` evaluates exit code 0, execute `--merge` directly. Do not stop to ask the owner for merge permission.
5. **Environment and reserved authority:** Across all repositories, an AI agent may merge changes that trigger staging deployment, temporary canaries, or preview redeployment. **Production reservation.** The owner approves every production change, for the precise commit or tag in question, and is present when it runs. A production change is anything that alters what production runs or stores: a release, promote or apply; a secret value; a manual data write or deletion; DNS, routes or the host; and whatever this repository's release pipeline names as production. A "deploy" reply authorizes only the release it names, and the dispatching session first hears from the owner directly. An edit that weakens this block, or the gate code and wording behind it, also needs the owner. Until the owner signs off an unbypassable production lock, through the commit of the PR that drops this sentence (tracked in infra2 #1035), the owner also approves edits to any workflow, to merge-gate code or data, and to code that a release workflow runs, even when a gate calls the edit a proven tightening. This repository also names as production: L1 bootstrap self-update, a runner rebuild (the `bootstrap` job of `deploy.yml`, whatever its `type` input), and the observability apply. One definition of production is pending (#1035). The gate's preflight refuses a checkout whose merge rules differ from main (#873), so main's rules judge every PR. `tools/pr_merge_gate.self_governing_files()` computes the gate's closure: `pr_merge_gate`, its imports, the data files it reads, their tests, every workflow, and the two rule texts. Until a production lock exists that the agent cannot bypass (#1035), a change to the closure needs the owner, since that code is what classifies a merge as production-bound.

   **Three-stage lifecycle -- merge != deploy (owner instruction, 2026-09-25):** Code changes in this repository pass through three physically distinct stages with zero overlap:
   - **Stage 1 -- Code Merge:** `git merge` lands commits on `main`. Zero container restarts; zero impact on running services.
   - **Stage 2 -- Staging Deploy:** Triggered exclusively by pushing a release tag `vX.Y.Z`. Auto-deploys to staging (soak). Prod is NOT touched.
   - **Stage 3 -- Production Deploy:** Requires an explicit separate action (`deploy.yml type=prod` or `reconcile-iac-inputs.yml promote_prod=true`). Tag push never auto-deploys prod.

   **Prod Gatekeeper protocol (mandatory before any session close on service-touching tasks):** Before closing or declaring a task complete, the agent MUST proactively report all three stage statuses -- (a) merge status with SHA, (b) staging deploy evidence (run URL + soak result), (c) current prod state (container image hash / Cloudflare watchdog heartbeat) -- and explicitly ask: "Authorize production deployment?" Then finish the non-production work. The issue stays open, labelled `prod-pending`, naming the release tag or commit, until the owner replies. A reply authorizes only the named release, and the owner attends its run. Once the owner grants prod authorization, the agent owns the ENTIRE deployment loop including physical verification (Touch Reality probe), and must never ask the owner to run commands manually.
6. **Closure changes** (owner instructions, 2026-09-22 and 2026-10-06). Until the owner approves the rule change that lifts the production-lock exception (#1035), every change to the closure or to release code goes to the owner, even one the gate proves tighter, because the workflow proof does not read job bodies. After the lock, a proven tightening of `docs/ssot/ci-gate-inventory.yaml` merges under the gate, and any other closure change follows a gate-change checklist: a gate checkout equal to main, a `### Mutation evidence` section that names the tests (`test_<name>` or `path::name`), and an automated review of the head. `libs/gate/self_governance.py` owns `_direction_proof_for()`; do not duplicate its file list here.

## Security and hard boundaries

- Never commit sensitive files (`*.pem`, `.env`, `*.tfvars`).
- On an infrastructure Apply conflict, follow the [State Discrepancy Protocol](docs/ssot/ops.standards.md#rule-4-状态不一致协议-state-discrepancy-protocol).
- 1Password is the only authority for static secrets.
- Surface any downtime risk. If downtime is unavoidable, provide a plan to shorten it.

## Load on demand

[`docs/README.md`](docs/README.md) is the single navigation index; do not copy its directory tree here.

| Task | Read |
|---|---|
| Overall engineering overview and quick start | [`README.md`](README.md) |
| Open a PR, decide merge readiness, or merge | [`docs/ssot/ops.merge-gate.md`](docs/ssot/ops.merge-gate.md) |
| Write code/docs, split tasks with STAR, or apply operating principles | [`docs/ssot/core.engineering.md`](docs/ssot/core.engineering.md) |
| Find technical truth, architecture, or SOPs | [`docs/ssot/README.md`](docs/ssot/README.md), generated from `MANIFEST.yaml` |
| Find the current task | [`docs/project/README.md`](docs/project/README.md) |
| Integrate an App or onboard | [`docs/onboarding/README.md`](docs/onboarding/README.md) |
| Change an infrastructure layer | Its README: [bootstrap](bootstrap/README.md), [platform](platform/README.md), [tools](tools/README.md), or [libs](libs/README.md) |

`CLAUDE.md` is a committed symlink to `AGENTS.md`. Both are generated from the owner's rule source: propose rule changes there instead of editing this file. Claude Code can read `AGENTS.md` natively since v2.1.277, but when a `CLAUDE.md` exists in the current directory or an ancestor it reads only `CLAUDE.md` files, so this repository must carry its own committed `CLAUDE.md`. On Windows without `core.symlinks`, a checkout writes the link as a one-line file; `libs/tests/test_claude_md_carrier.py` fails loudly on that shape.
<!-- WS_STATIC_END -->
