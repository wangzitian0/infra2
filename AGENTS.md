<!-- WS_STATIC_START adapter=rules-v2 inputs=b670053f4b0b6210f560d7fb528809967bee0e13a67b4c35bc58b7713c346aba -->
<!-- Generated file: do not edit by hand. These rules are maintained in the owner's rule source and re-rendered here. -->

## Engineering discipline

- **Measure the physical system first.** Before an abstract architecture proposal, inspect the system with read-only probes such as `time cmd`, process chains, and file-descriptor locks. A conceptually neat story without physical evidence is insufficient. A probe must not write. Do not combine validation and action in one command: a POST permission probe can create a resource, and a trial commit can leave a real commit. Measure, read the result, then decide, with a stop point between these steps.
- **Green does not prove truth.** The tested system writes its own unit tests, CI, and issue states. Cross-check critical conclusions against two external sources not written by this repository.
- **Tests must be falsifiable.** Do not hide assertions in `if (exists)` or `if (code != 0)` so failures run zero assertions. Do not accept tautologies such as `typeof null === 'object'` or `result !== undefined || true`. Source-text `indexOf` matches against prose are not integration tests. Duplicate test function identifiers can silently shadow earlier tests (Python `def` and duplicate JS function/const/export names); duplicate string titles in `test()` or `it()` instead run both. A green count is not coverage evidence. Make a new test fail under a relevant mutation. A read-only reviewer treats such fake-test patterns as CRITICAL merge blockers.
- **One source of truth:** Keep one authoritative definition for each core fact. Repeated hardcoding and scattered configuration invite drift.
- **Clean up during migration:** After the new mechanism is live and equivalence is proved, remove its predecessor, obsolete files, and dead code in the same change. Define contracts first and derive CI from them.
- **Deletion can leave guards green and empty:** A guard for an old structure can stop checking anything after deletion. Check each guard and remove it or redirect it to the new structure; green tests alone do not prove safe deletion.
- **Define guard scope from what it must govern, not from today's passing tree.** Let the guard fail on existing violations, then repair them. A guard never seen failing is not yet evidence of protection.

## Delivery and merge

- **Fail fast left to right:** Put the cheapest and likeliest failure checks first.
- **Review standing authorization:** Resolve a review thread directly after independently verifying it is fixed or obsolete. Do not resolve actionable, ambiguous, or unverified feedback. Automated reviewers may read a redacted GitHub diff rather than source: GitHub can show `"Authorization": f"Bearer ******"` where source has `"Authorization": f"Bearer {token}"`. Check source before judging a report. When a report is false, turn the concern into a falsifiable invariant test rather than merely dismissing it.
- **Weighted review gates:** Each repository defines its own severity weights and blocking thresholds. Read literal `severity: <level>` tags; do not infer severity from prose.
- **Merge when ready:** Once all merge conditions pass, merge and continue from the latest main rather than piling up divergent branches.

## Runtime safety

- **Reason from the worst case.** For environment changes, wrappers, redirection, or interception rules, check for no-TTY deadlock in CI or child processes, concurrent shared-file truncation/races, and network or cold-start failure cascades. Reject a proposal whose lack of backlash cannot be established.
- **Treat three hidden green failures as defects:** WRONG FORMULA (an incorrect formula passes assertions), GREEN-WHILE-EMPTY (filtering removes all output but reports success), and STALE-REPORTED-AS-FRESH (old data is labeled fresh). Implausible output is evidence of a defect.
- **Protect ambient services.** Default unit tests and Executor tasks must not destructively act on host ports, shared background processes, or development databases (`DROP`, `TRUNCATE`, `--clear`, forced restart). Resets require an isolated sandbox/worktree with a dedicated random port, or an explicit `CI=true` or `ALLOW_CLEAR_TEST=1` guard; otherwise skip safely with a warning.
- **Two triggers:** Every background job or batch process needs both scheduled execution and manual replay.
- **Do not steal CD locks:** A trigger is instant but publication is delayed. Do not interrupt a running deployment; the next run must coalesce commits accumulated while it was busy.

# Infra2 Harness and Infrastructure AI Agent Rules

> **Authority boundary:** An AI agent may modify this file only under an explicit owner instruction, which the PR description must quote. The agent may merge a PR when every merge gate passes (standing authority is a Workspace-tier fact; this repository's criteria are in [`docs/ssot/ops.merge-gate.md`](docs/ssot/ops.merge-gate.md)). If any required state fails, is missing, or cannot be verified, fail closed and do not merge.

> **Keep only decision invariants here.** Load procedures on demand through the links below. Long always-loaded instructions dilute hard boundaries.

## Harness scope and precedence

This repository is both the implementation/deployment control plane for `infra2` and a multi-repository development workspace. The machine inventory is [`harness/repos.yaml`](harness/repos.yaml); architecture boundaries are in [`docs/ssot/core.harness.md`](docs/ssot/core.harness.md).

1. **Harness focus:** This file directly governs `infra2` work at the root. Workspace emphasis covers `infra2`, `infra2-sdk`, and general collaboration preferences.
2. **App autonomy:** `repos/finance_report` and `repos/truealpha` are workspace checkouts. On entering an App, read its own `AGENTS.md` and architecture documents; the App's local rules take precedence. Each App's Repo tier is projected from its own dev_env source; the harness does not define or overwrite App policy. **Merge authority is not App policy:** it is a standing owner grant across their repositories and does not need to be requested again on changing repositories.
3. **Workspace tooling:** Root-level `oh-my-code-agent/` is an independent submodule for TUI management. It evolves independently and must not become a runtime or source dependency of infra or Apps.
4. **Preferences are not cross-repository commands:** Default GitHub, collaboration, and design preferences live in [`harness/workspace/`](harness/workspace/). More specific rules in the target repository prevail.
5. **Dependency boundary:** A submodule is a development snapshot, not a package, runtime, deployment, or configuration-hash dependency. Stable cross-repository code contracts travel only through a published `infra2-sdk` version.

## Core mandatory principles (SSOT first)

1. **SSOT is authoritative:** [`docs/ssot/`](docs/ssot/README.md) is the only authority for infrastructure facts.
2. **Define truth before implementation:** Specify a new component's architecture, constraints, and SOP in `docs/ssot/` before implementing it.
3. **No hidden drift:** When code and SSOT disagree, correct the mismatch immediately; do not let the SSOT decay.

## Merge gate (minimum decision criteria)

Full procedures are in [`docs/ssot/ops.merge-gate.md`](docs/ssot/ops.merge-gate.md). **Any unmet criterion below fails closed and prohibits merge:**

1. **Same head:** Checks, review, and merge must refer to the same `head SHA`. Old local results or stale reviews are not substitutes.
2. **Merge Authority green:** Every applicable check marked `blocks_merge: true` in [`docs/ssot/ci-gate-inventory.yaml`](docs/ssot/ci-gate-inventory.yaml) must conclude success. Pending, failed, cancelled, unexpectedly skipped, or unreadable counts as unmet.
3. **Review closure:** Weight unresolved findings by literal severity: high=1.0, middle=0.5, low=0.25, and unlabeled=middle. A total of at least 1.0 blocks merge. Let `pr_merge_gate` calculate it; do not estimate manually. Only a literal `severity: <level>` annotation defines severity; prose tone does not.
4. **Green must be current and actually reported:** A sibling PR can alter the same files after this PR's checks without triggering a rerun. GitHub may also accept a skipped required check. Use `python -m tools.pr_merge_gate <n> --policy either --request-review --merge` for the decision (exit 1 means not ready; exit 2 means owner action required), rather than visually reading a check table.
5. **Environment and reserved authority:** Across all repositories, an AI agent may merge changes that trigger staging deployment, temporary canaries, or preview redeployment. Only **production deployment** is reserved to the owner and requires approval for the current `head SHA`: production apply/promote, L1 bootstrap self-update, runner rebuild, and observability apply. A separate reason to return to the owner is **self-adjudication**: changing what decides whether this very PR may merge. The gate reads rules from the worktree; a PR could otherwise judge itself under rules it introduced. The protected set is a computable dependency closure: `pr_merge_gate`, all its imports, the data files it reads, and the tests for those files. `tools/pr_merge_gate.self_governing_files()` computes that closure so a new import is protected automatically. Only two rule-text files outside the code-readable closure are listed explicitly: this file and `ops.merge-gate.md`.

   **Three-stage lifecycle — merge != deploy (owner instruction, 2026-09-25):** Code changes in this repository pass through three physically distinct stages with zero overlap:
   - **Stage 1 — Code Merge:** `git merge` lands commits on `main`. Zero container restarts; zero impact on running services.
   - **Stage 2 — Staging Deploy:** Triggered exclusively by pushing a release tag `vX.Y.Z`. Auto-deploys to staging (soak). Prod is NOT touched.
   - **Stage 3 — Production Deploy:** Requires an explicit separate action (`deploy.yml type=prod` or `reconcile-iac-inputs.yml promote_prod=true`). Tag push never auto-deploys prod.

   **Prod Gatekeeper protocol (mandatory before any session close on service-touching tasks):** Before closing or declaring a task complete, the agent MUST proactively report all three stage statuses — (a) merge status with SHA, (b) staging deploy evidence (run URL + soak result), (c) current prod state (container image hash / Cloudflare watchdog heartbeat) — and explicitly ask: "Authorize production deployment?" Closing silently without obtaining an explicit prod disposition (either "deploy" or "hold") is forbidden. Once the owner grants prod authorization, the agent owns the ENTIRE deployment loop including physical verification (Touch Reality probe), and must never ask the owner to run commands manually.
6. **Judge self-adjudication by direction, not merely by file** (owner instruction, 2026-09-22): The risk is a change favorable to the PR itself. A mechanically proven tightening can proceed under the gate; a relaxation or a change whose direction cannot be proven returns to the owner. Proof must be computed, not asserted in a PR description. It is possible only for closed-set decision inputs with mechanically comparable base and head values. `tools/pr_merge_gate.py` owns `_direction_proof_for()`; do not duplicate its file list here. Python and rule prose can loosen behavior in arbitrary ways and therefore return to the owner. Unknown files, unreadable input, parse failure, or missing base input also return to the owner by default. A protected rule-text change must cite the owner's instruction.

   This prose once disagreed with implementation by being stricter (#855). The strict direction was still wrong: one reader unnecessarily asked the owner, while another reader followed code. A rule that requires reading implementation again to interpret it has failed as a rule. The mechanical criterion now lives in code; this paragraph points to it.

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
