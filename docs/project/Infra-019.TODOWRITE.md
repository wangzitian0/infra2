# Infra-019: TODOWRITE (Workspace Harness Control Plane)

**Status**: Active
**Last Updated**: 2026-09-16

## Phase 1

- [x] Define infra2/infra2-sdk focus and autonomous App boundary in SSOT.
- [x] Add machine-readable `harness/repos.yaml`.
- [x] Add workspace coordination, GitHub, and software-design preferences.
- [x] Add read-only `tools.harness check` with unit tests.
- [x] Route root README, AGENTS, repos, libs, and tools documentation.

## Follow-Ups

- [x] Add root-level `oh-my-code-agent` submodule as coordinated workspace TUI tooling.
- [x] Pin `oh-my-code-agent` to its design-complete main head (`5b8ae03`) and switch the
      submodule URL to HTTPS so `git submodule update --init` works without SSH keys.
      Design revision PR: wangzitian0/oh-my-code-agent#4 (plugin-based adapters,
      Claude Code + Codex first-party, isolated launch first); re-pin after it merges.

- [x] Add read-only `harness status` for parent pin, checkout/remote head,
      ahead/behind, dirty state, and release identity; optional fetch changes refs only.
- [x] Add an infra2-sdk-local contributor guide through independent SDK PR #31
      (merged and released in v1.5.2; installed-artifact proof below).
- [ ] Decide whether workspace preference changes need their own version identifier.
- [ ] Add cross-repository compatibility matrix reporting from released evidence.
- [x] Orchestrator liveness rule, read-only `harness sweep` and guard hook
      (2026-09-16 section below).
- [ ] Owner decision: wire `tools/orchestrator_guard_hook.py` into `.claude/settings.json`.
- [ ] Split `pr_merge_gate` exit 1 into time-fixable / action-required / could-not-evaluate
      ([infra2#740](https://github.com/wangzitian0/infra2/issues/740)).
- [ ] Archive Infra-019 after the selected follow-ups are complete or explicitly deferred.

> App policy adoption is intentionally not a TODO. Finance Report and TrueAlpha remain
> autonomous and may independently reuse a preference if their maintainers choose.

## 2026-09-15 foundation review

Outcome: each repository retains one architecture owner, executable local checks and
an independent release, with common runtime semantics consumed from the released SDK.

Fetched and fast-forwarded all five checkouts. Review baseline: infra2 `8d5e62f`, SDK
`50439e3` (v1.5.1), Finance Report `4e2d0735`, TrueAlpha `567bbba`, OMCA `58f1ad9`.
The pre-existing root `handover.md` and OMCA working changes were retained.

### Findings and actions

- [x] Workspace authority was incomplete: OMCA charter/agent rules and Finance package
  ownership were missing from the inventory. Add current entry points and align the
  SDK protocol-adapter versus infra orchestration boundary.
- [x] An empty optional submodule could be reported/fetched as its parent repository.
  Verify the Git top-level before observation; a real Git fixture proves the failure.
- [x] TrueAlpha duplicated the SDK tier enum and rejected canonical tier names.
  Independent [TrueAlpha #820](https://github.com/wangzitian0/truealpha/issues/820)
  change adopts the existing released enum/parser through its public
  compatibility import path; dependency instances remain app-owned.
- [x] Finance SDK wheel coordinates were hand-maintained in three workflows.
  Independent [Finance Report #2004](https://github.com/wangzitian0/finance_report/issues/2004)
  change reads the generated backend lock and checks its agreement
  with the dependency declaration; checksum enforcement stays at acquisition.
- [x] SDK silently ignored missing/nonmapping named override tables. Independent SDK
  patch rejects them before writing a manifest, with failing-first regression tests.
- [ ] Merge the independent reviewed changes and update the integrated snapshot after
  their applicable checks pass. No new SDK release or production deployment is implied.

### Initial OMCA evidence and limitations (before repairs)

The existing candidate `go run ./cmd/omca qualify tui --json` was exercised with no
interactive/model session. Codex 0.153.4: no qualified Knowledge Pack; MCP and Skill
introspection fail. Claude Code 2.1.267: MCP inclusion/exclusion passes; Skill inventory
and initial/restart TUI/model canary remain UNKNOWN. The probe reported unchanged native
configuration roots. This does not establish full MVP usability or authenticate a host.

The installed CLI lacks `--help`. The Go suite also revealed an ambient `CODEX_HOME`
leaking into an unmanaged MCP fixture. A separate OMCA worktree isolates these fixes
from the user's uncommitted qualification work. Host-version qualification and the
human-owned interactive proof remain explicit follow-ups, not inferred success.

### Deliberate boundaries / counterfactual

Finance's package contracts and TrueAlpha's issue acceptance are intended differences.
Do not recreate a shared `docs/ssot/` layout or centralize product policy. Finance's
environment generator retains legacy envelope/filter semantics; replacing it wholesale
with SDK `manifests.main` requires an explicit compatibility proof first. TrueAlpha's
KG probes and domain manifests retain application policy. Structural checks do not
prove either application's deployed behavior. No product issue is closed on this review.

### Initial review delivery and proof (superseded by continuation below)

| Repository | Independent PR | Evidence / remaining boundary |
| --- | --- | --- |
| infra2 | [#699](https://github.com/wangzitian0/infra2/pull/699) | 29 harness/adoption tests; workspace inventory check and documentation build pass. CI passed at `9785316`; this evidence update needs a fresh head check. |
| infra2-sdk | [#31](https://github.com/wangzitian0/infra2-sdk/pull/31) | 308 tests, 94.43% coverage; all ten remote checks pass at `9f44145`. Candidate version 1.5.2 is not released. |
| TrueAlpha | [#821](https://github.com/wangzitian0/truealpha/pull/821) | 14 scoped runtime tests and prepush pass at `21be729`; remote Python gates, Dagster liveness and application image builds pass. Full CI is blocked on MinIO acquisition. |
| Finance Report | [#2036](https://github.com/wangzitian0/finance_report/pull/2036) | 36 local pin/workflow/common-tooling tests, lint/typecheck and static preflight pass. Remote CI exposed the new wrapper's mismatch with the common CLI contract; the corrected wrapper passes that existing regression gate. Full remote revalidation remains required; integration and Tier-1 E2E also stop on MinIO acquisition. |
| OMCA | [#91](https://github.com/wangzitian0/oh-my-code-agent/pull/91) | Go race tests, build and lint pass; all four remote checks pass at `0b3dce7`. Original checkout and installed binary are unchanged. Full host qualification remains incomplete as above. |

The shared CI blocker is `minio/minio:RELEASE.2025-09-07T16-13-09Z`: Docker
reports pull access denied before tests start. TrueAlpha's core/data-engine/runtime
jobs and Finance's integration/Tier-1 jobs cannot use the configured image. This
establishes acquisition failure, not the registry-side cause. Track restoration in
[TrueAlpha #823](https://github.com/wangzitian0/truealpha/issues/823); retain real
object-store checks and verify any replacement artifact's provenance. No production
storage migration is part of these structural changes.

PR check status is evidence for the stated head, not merge authorization. Integrated
submodule pins remain unchanged until the independent changes are reviewed and merged.

## 2026-09-15 authorized usability continuation

Owner authorized quality-gated merges for this session and set the acceptance target:
OMCA and the SDK must be useful independently, reducing app setup and maintenance cost.
This extends the initial review above; it does not override app architecture ownership.

- SDK #31 merged as `794544d157e5ac95cfb17b1b253c6f8e277dd7c5`; post-merge CI passed.
  Released `v1.5.2` from that reviewed main commit. Release run `34935463086` passed.
  Downloaded the published wheel into a fresh environment outside all repositories;
  SHA256 `e422846adab5fb25818e8722f06a71a78de2b09527c28116533bf6a0d1e48ec8`
  matched the release. Core/S3/Postgres/HTTP/OTel/all smoke groups and installed dependency
  compatibility passed. This is installed-artifact proof, not a sibling-source import.
- OMCA #91 merged as `a241446c430cb088092fc526c8ba59e618f80fa7`; post-merge CI passed.
  The original dirty OMCA checkout remains untouched. Candidate work is isolated in a
  separate worktree and submitted as [#93](https://github.com/wangzitian0/oh-my-code-agent/pull/93).
- Codex 0.153.4 rejected the old generated approval setting. OMCA #93 adopts the documented
  untrusted-project migration with the existing read-only sandbox default, and versions
  both bootstrap/full-generation cache identities. Real safe introspection now proves
  OMCA MCP inclusion, repository Skill inclusion, native sentinel exclusion, and unchanged
  native configuration snapshots. Claude 2.1.267 proves MCP inclusion/exclusion. All Go
  race tests and lint pass; coverage is 80.4%. Human TUI/restart/model proof and Claude
  Skill inventory remain UNKNOWN; this does not establish full interactive MVP acceptance.
- TrueAlpha #821 was rebased onto main including the independently delivered MinIO
  mirror fix [TrueAlpha #825](https://github.com/wangzitian0/truealpha/pull/825).
  All applicable checks passed at `50bee8a`; a subsequent review nit
  is being fixed and requires another current-head CI/review pass.
- Finance #2036 now also restores both MinIO server/client acquisition through immutable
  upstream Quay artifacts under
  [Finance Report #2037](https://github.com/wangzitian0/finance_report/issues/2037).
  Its existing toolchain gate now covers each CI
  acquisition job and preview Compose. Six drift mutations failed against the old guard;
  all 14 toolchain tests and static preflight pass after the repair. Remote CI must prove
  actual image acquisition because Docker is unavailable in the local workspace.
- The SDK gets a standalone app readiness example with an installed-wheel HTTP smoke:
  healthy, unhealthy, and omitted required dependency cases exercise the documented
  public entrypoint. Dependency names, required tiers, and business policy remain app-owned.

Remaining: finish current-head review/checks, merge eligible PRs, verify post-merge runs,
and update parent snapshots only to reviewed main commits. Human-only host evidence
must stay visibly incomplete until actually supplied. No production promotion is planned.

## 2026-09-15 integrated acceptance evidence

This section supersedes the initial review's pending states above. Shared structure means
clear ownership, a local verification entry, a versioned consumption boundary, and
independent delivery. It does not require matching App directory trees.

### Delivered repository changes

- infra2 [#699](https://github.com/wangzitian0/infra2/pull/699) merged as
  `f0f687b4dfb9cdbb10345144baf52bc6f0e282e7`. Current-head reviews/threads and applicable
  checks passed before merge; post-merge Infrastructure CI `34938241862` and Docs
  `34938241871` passed. The shared inventory now points to each repository's actual
  authority, and an empty submodule cannot masquerade as the parent Git repository.
- SDK [#31](https://github.com/wangzitian0/infra2-sdk/pull/31) is released as `v1.5.2`
  with the installed-artifact proof recorded above. Documentation/example
  [#32](https://github.com/wangzitian0/infra2-sdk/pull/32) merged as
  `3fecc8e48707a296f0f26315ba14106dd86f9e6b` after all ten checks and exact-head review.
  Its post-merge CI `34939096142` passed.
  The app-root readiness example proves healthy, unhealthy and missing dependency
  outcomes using the released wheel; it requires no sibling repository source.
- TrueAlpha [#821](https://github.com/wangzitian0/truealpha/pull/821) merged as
  `e1cda5fbbc6665359e139cd9b331a032363b1b12`. Its public compatibility module delegates
  environment semantics to the released SDK. Fourteen scoped tests and all applicable
  remote checks passed. The subsequent reviewed main `a65e373a8f3c6900e2c926bcdda7afded6d26d9c`
  includes this change; its CI `34937462324` passed after superseding the cancelled
  earlier main run. The independently merged
  [TrueAlpha #825](https://github.com/wangzitian0/truealpha/pull/825) restored MinIO acquisition.
- OMCA [#91](https://github.com/wangzitian0/oh-my-code-agent/pull/91),
  [#93](https://github.com/wangzitian0/oh-my-code-agent/pull/93), and
  [#95](https://github.com/wangzitian0/oh-my-code-agent/pull/95) are merged.
  The final main `fed892890cee650bd6798f467ba5813655a02c4a` passed post-merge CI
  `34939043207`. Besides help and current-host compatibility, shell entry and direct
  launch now preserve explicitly activated profiles and activation evidence. Failing-first
  entrypoint tests cover both paths; incompatible state fails without resetting it.
- Finance Report [#2036](https://github.com/wangzitian0/finance_report/pull/2036) merged
  as `39e8ecf08497764c10aa308409d60041f501d398` after exact-head review, all applicable
  checks, and thread closure. PR CI `34938808207` and Preview `34938808204` passed:
  all five backend shards, integration, Tier-1 API E2E, frontend build/browser/coverage,
  tooling coverage, unified coverage, traceability, and the behavioral score ratchet.
  The SDK pin helper removes three workflow-owned coordinate copies. Real CI proves
  the version-preserving immutable MinIO mirror repair; mutation tests cover both
  acquisition jobs and preview Compose. Main CI is
  [run 34939649716](https://github.com/wangzitian0/finance_report/actions/runs/34939649716).
  This closes acquisition incident
  [Finance Report #2037](https://github.com/wangzitian0/finance_report/issues/2037).
  The broader SDK bump and dispatch-correlation proof in
  [Finance Report #2004](https://github.com/wangzitian0/finance_report/issues/2004)
  remains separately tracked; this change delivers its single-source pin scope.
- Finance Report's main run above then exposed a measurement gap: its component coverage
  ratchet blocks on main but only reports on PRs. The SDK CLI's `python -S` subprocess
  proof was outside pytest-cov, leaving four CLI function lines unmeasured. Follow-up
  [Finance Report #2038](https://github.com/wangzitian0/finance_report/pull/2038) adds
  in-process proof of complete successful output and zero partial output on invalid
  arguments/lock data, while retaining the subprocess isolation check. It merged as
  `7c13b87cfb4be4a7f632d60fe6468a07eb9cb924` after all checks and exact-head review.
  PR CI `34940605397` measured tools at `3129/3386` (92.41%), above the unchanged
  `3065/3318` (92.37%) baseline; the failing main had measured `3125/3386` (92.29%).
  No threshold, baseline, or runtime behavior changed. The replacement main evidence is
  [run 34941437376](https://github.com/wangzitian0/finance_report/actions/runs/34941437376);
  the parent snapshot's merge checklist requires completion of that run.

The integrated development pins are SDK `3fecc8e`, OMCA `fed8928`, TrueAlpha `a65e373`,
and Finance Report `7c13b87c`. Each full commit is identified above. The parent integration
PR is [infra2 #705](https://github.com/wangzitian0/infra2/pull/705); its merge requires
fresh Merge Authority checks, review closure and the session's twelve-minute quiet window.

### Installed OMCA proof and outstanding human gate

Built the reviewed main above and atomically installed it at `~/.local/bin/omca`, reporting
`omca dev+fed8928`. SHA256:
`ed75ccbb64c718e296503cfc8fefc50ecfe08cab9c0c6617a86c6b74ed39342b`.
The prior binary is retained at `~/.local/bin/omca.before-foundation-review-20260915`.
This is a pinned development build, not an invented release tag.

Installed-binary `omca qualify tui --json` at `2026-09-15T06:55:19Z` reported:

| Host | Knowledge pack | MCP isolation | Skills isolation | TUI/restart/model |
|---|---|---|---|---|
| Codex 0.153.4 | PASS | PASS | PASS: managed sentinel present, native sentinels absent | UNKNOWN |
| Claude Code 2.1.267 | PASS | PASS: OMCA connected, native sentinel absent | UNKNOWN | UNKNOWN |

Native configuration snapshots were unchanged; `interactiveAttempted=false` and
`complete=false`. Exit 1 reflects the incomplete human gate. No model call was made.
The original JSON is retained below; it contains no credentials or native file contents.

<details>
<summary>Installed-binary qualification receipt</summary>

```json
{
  "kind": "InteractiveTUIQualification",
  "generatedAt": "2026-09-15T06:55:19Z",
  "hosts": [
    {
      "host": "codex",
      "version": "0.153.4",
      "knowledgePack": "codex:cli:0.153.4",
      "checks": [
        {
          "id": "knowledge-pack",
          "status": "PASS",
          "evidence": "E2",
          "detail": "installed host version is covered by codex:cli:0.153.4"
        },
        {
          "id": "native-mcp-exclusion",
          "status": "PASS",
          "evidence": "E3",
          "detail": "host-reported MCP inventory=[omca]; native sentinel absent"
        },
        {
          "id": "skill-isolation",
          "status": "PASS",
          "evidence": "E3",
          "detail": "Codex host-reported 7 visible Skills; managed repository sentinel present and native sentinels absent"
        },
        {
          "id": "human-interactive-tui",
          "status": "UNKNOWN",
          "evidence": "E0",
          "detail": "codex initial/restart TUI and omca_status model canary require --interactive and explicit human attestation"
        }
      ],
      "complete": false
    },
    {
      "host": "claude-code",
      "version": "2.1.267",
      "knowledgePack": "claude-code:cli:2.1",
      "checks": [
        {
          "id": "knowledge-pack",
          "status": "PASS",
          "evidence": "E2",
          "detail": "installed host version is covered by claude-code:cli:2.1"
        },
        {
          "id": "native-mcp-exclusion",
          "status": "PASS",
          "evidence": "E3",
          "detail": "Claude host report shows omca connected and the native sentinel absent"
        },
        {
          "id": "skill-isolation",
          "status": "UNKNOWN",
          "evidence": "E1",
          "detail": "Claude Code 2.1.267 exposes no safe non-interactive Skill inventory; run --interactive under human supervision and verify /skills shows the managed repository sentinel but not the native sentinel"
        },
        {
          "id": "human-interactive-tui",
          "status": "UNKNOWN",
          "evidence": "E0",
          "detail": "claude-code initial/restart TUI and omca_status model canary require --interactive and explicit human attestation"
        }
      ],
      "complete": false
    }
  ],
  "realNativeStateClean": true,
  "interactiveAttempted": false,
  "complete": false
}
```

</details>

The owner has been asked to run the documented human procedure from their own terminal.
Full interactive usability remains unaccepted until that evidence exists.

The original dirty OMCA checkout and root `handover.md` are preserved. Its reviewed code
was integrated through isolated worktrees; the original checkout was not reset or
overwritten. Parent gitlinks describe reviewed snapshots, not runtime dependencies.

### Independent operational work

Ops run `34938044492`, predating infra2 #699's merge, failed while importing the SDK through the
Vault audit's registry dependency. The independent
[infra2 #703](https://github.com/wangzitian0/infra2/pull/703) merged as `b6c7f239`;
the Vault self-refresh audit in
[run 34939451271](https://github.com/wangzitian0/infra2/actions/runs/34939451271) passed
on that main commit. The earlier import failure did not establish a service outage.
No infrastructure apply or production promotion is part of this delivery.

## 2026-09-15 standalone OMCA follow-up

This section supersedes the installed OMCA version/proof above after the local
Codex upgrade. The distribution boundary is now part of the harness acceptance:
SDK consumption uses a released artifact outside the repository; OMCA must run
without its build checkout. Human host interaction remains a separate gate.

- OMCA [#96](https://github.com/wangzitian0/oh-my-code-agent/pull/96) merged as
  `751ddc54b42647abd77403fe75d002d8662c7d45`. It adds the exact Codex 0.154.0
  observation pack and preserves adjacent-version rejection. Post-merge
  [CI 34954115739](https://github.com/wangzitian0/oh-my-code-agent/actions/runs/34954115739)
  passed.
- OMCA [#98](https://github.com/wangzitian0/oh-my-code-agent/pull/98) merged as
  `fa0455838d80ab13da156b6e567317919b525b53`. Knowledge and ontology JSON are
  embedded from their canonical files. A trimmed-path CLI report and default
  ontology lookup failed before the fix and pass after it; CI includes the
  standalone gate. Post-merge
  [CI 34955431174](https://github.com/wangzitian0/oh-my-code-agent/actions/runs/34955431174)
  passed, including the full race suite. Both PRs passed current-head review,
  with all review threads resolved before merge.
- Built the merged commit from a temporary source archive with
  `go build -trimpath`, removed that build source, and atomically installed the executable
  at `~/.local/bin/omca`. The installed version is `dev+fa04558`; SHA-256 is
  `b6682e2d23ea4f1ecb231b3a5aa4e9c32b131f5077937efaa520133951d2325b`.
  The previous executable is retained as
  `~/.local/bin/omca.before-standalone-20260915`.
- At `2026-09-15T10:00:19Z`, that installed executable ran `omca qualify tui --json`
  outside the checkout: Codex 0.154.0 Knowledge/MCP/Skills passed; Claude Code
  2.1.272 Knowledge/MCP passed. Native configuration snapshots remained equal.
  The full safe artifact is below; expected exit 1 reflects the remaining human
  gate and Claude's unavailable automatic Skill inventory, not a passed full TUI.
- The SDK's fresh standalone proof at `2026-09-15T07:59:41Z` downloaded the
  published v1.5.2 wheel, verified SHA-256
  `e422846adab5fb25818e8722f06a71a78de2b09527c28116533bf6a0d1e48ec8`, and
  installed all extras into a new environment outside every repository.
  Dependency compatibility and all six smoke groups passed; the HTTP app
  entrypoint covered healthy, unhealthy and missing dependency outcomes.
  A separate current-source run passed all 308 SDK tests.

The OMCA gitlink now pins `fa0455838d80ab13da156b6e567317919b525b53`. The original
OMCA checkout's pre-existing edits remain preserved, so its worktree can still
show behind/dirty status; the installed tool has no source-checkout dependency.

Remaining acceptance: a human runs the documented Codex and Claude interactive
qualification against the installed executable and supplies its initial/restart
TUI, Skill inventory and model-canary outcomes. Automatic evidence does not
complete that requirement, and Infra-019 remains In Progress.

```json
{
  "kind": "InteractiveTUIQualification",
  "generatedAt": "2026-09-15T10:00:19Z",
  "hosts": [
    {
      "host": "codex",
      "version": "0.154.0",
      "knowledgePack": "codex:cli:0.154.0",
      "checks": [
        {
          "id": "knowledge-pack",
          "status": "PASS",
          "evidence": "E2",
          "detail": "installed host version is covered by codex:cli:0.154.0"
        },
        {
          "id": "native-mcp-exclusion",
          "status": "PASS",
          "evidence": "E3",
          "detail": "host-reported MCP inventory=[omca]; native sentinel absent"
        },
        {
          "id": "skill-isolation",
          "status": "PASS",
          "evidence": "E3",
          "detail": "Codex host-reported 7 visible Skills; managed repository sentinel present and native sentinels absent"
        },
        {
          "id": "human-interactive-tui",
          "status": "UNKNOWN",
          "evidence": "E0",
          "detail": "codex initial/restart TUI and omca_status model canary require --interactive and explicit human attestation"
        }
      ],
      "complete": false
    },
    {
      "host": "claude-code",
      "version": "2.1.272",
      "knowledgePack": "claude-code:cli:2.1",
      "checks": [
        {
          "id": "knowledge-pack",
          "status": "PASS",
          "evidence": "E2",
          "detail": "installed host version is covered by claude-code:cli:2.1"
        },
        {
          "id": "native-mcp-exclusion",
          "status": "PASS",
          "evidence": "E3",
          "detail": "Claude host report shows omca connected and the native sentinel absent"
        },
        {
          "id": "skill-isolation",
          "status": "UNKNOWN",
          "evidence": "E1",
          "detail": "Claude Code 2.1.272 exposes no safe non-interactive Skill inventory; run --interactive under human supervision and verify /skills shows the managed repository sentinel but not the native sentinel"
        },
        {
          "id": "human-interactive-tui",
          "status": "UNKNOWN",
          "evidence": "E0",
          "detail": "claude-code initial/restart TUI and omca_status model canary require --interactive and explicit human attestation"
        }
      ],
      "complete": false
    }
  ],
  "realNativeStateClean": true,
  "interactiveAttempted": false,
  "complete": false
}
```

## 2026-09-16 orchestrator liveness

Outcome: the main conversation never waits silently. Every in-flight agent, PR, release
and default-branch CI run is on one watch list, a persistent `Monitor` runs one read-only
sweep over it, and only allow-listed facts keep an item waiting
([truealpha#876](https://github.com/wangzitian0/truealpha/issues/876)).

Situation: the orchestrator waited more than ten minutes on a PR because its shell
waiter grepped a merge gate's prose and never matched "1 unresolved review thread(s)".
Notifications reach the main conversation only between tool calls, so a long
foreground wait is deaf for its whole length. `pr_merge_gate` exit 1 covers pending
and red checks, open threads, drafts and merged PRs alike, and `gh pr checks --json`
exits 1 with "no checks reported" before any check exists, which crashed the gate.

- [x] `libs/harness_sweep.py` + `python -m tools.harness sweep`: WAITING / DONE / ACTION /
      STALL / UNKNOWN per item, gates by exit code only, mutating gate flags (and argparse
      abbreviations of them) refused, `--watch` prints transitions and a heartbeat and
      exits when an item leaves WAITING. Usage and watch-list errors exit 4, never 2.
      Ported from a reviewed session prototype; stable per-item keys and a head-commit
      run query (`gh run list --commit`) were added.
- [x] Only prints: no alert-delivery primitive is called, so `tools/no_new_wheels_lint.py`
      needs no signal registration (lint passes).
- [x] `tools/pr_merge_gate.py`: gh's "no checks reported" is zero checks, so the existing
      "no checks reported yet" reason is printed; other gh failures still raise. The test
      fails on the previous code. Exit-code contract unchanged; the split is
      [infra2#740](https://github.com/wangzitian0/infra2/issues/740).
- [x] Rule: `harness/workspace/coordination.md` "Orchestrator Liveness", with stall
      thresholds and the settings snippet.
- [x] `tools/orchestrator_guard_hook.py` (stdlib only) with tests; not wired. The hook
      input fields it relies on (`agent_id` for subagents, `scratchpad_dir`,
      `stop_hook_active`) are present in the Claude Code 2.1.272 hook input schema.
- [x] Owner: add the documented hook entries to `.claude/settings.json` (done 2026-09-17: `.claude/settings.json` is a symlink projected by dev_env `workspace-iac/bin/ws-apply`, which now emits the hooks; the command exits 0 in projects without the hook file).
- [x] Workflow probe: newest of several listed runs, never older than a run already seen in the watch (2026-09-17: `gh run list --limit 1` returned the previous day's run and the watch printed WAITING->DONE for a run still in progress).

Live read-only smoke (2026-09-16, one-shot against real GitHub): an open infra2 PR with
pending checks reported WAITING, a merged PR DONE, a branch without a PR WAITING, a gate
argv containing `--mer` UNKNOWN before execution, and the sweep exited 4 accordingly.
One earlier sweep reported the main-branch head of a path-filtered workflow as having no
run although one existed; it did not reproduce, and the head-commit query now asks for
that commit's run directly.

Rollback: revert the PR. Nothing is deployed, applied or wired; no secret is read.

## 2026-09-16 host strategy: pi in, opencode out (omca)

### Artifacts

- PR wangzitian0/oh-my-code-agent#107 `chore/drop-opencode-host` — OpenCode 移出 closed host vocabulary（ID/schema/ontology/roadmap/runtime/charter + 负路径测试换 cursor）；关闭 upstream issue wangzitian0/oh-my-code-agent#31。CI 全绿。
- PR wangzitian0/oh-my-code-agent#108 `feat/pi-host-observation` — pi 成为第三个一级 host（检测+观察层）：`PI_CODING_AGENT_DIR` native home、pi --version 严格裸 semver 解析、user/workspace/directory-chain 规则（auth.json discoverOnly E0）、coverage 诚实表（mcp/hook UNSUPPORTED——经 extensions 实现代码，不翻译）、pi.dev 官方源 allowlist、`knowledge/hosts/pi/cli/0.85` pack、evidence-ceiling 双侧同步。CI 全绿。
- 真机 E2E（只读）：`omca context` 检出 pi 0.85.1；`omca report` 呈现 FRESH pack + 4 条真实观察（settings/trust E1、auth E0、worktree AGENTS.md E1）；零写入证明成立（唯一变化为本会话宿主 pi 进程自身的 session JSONL）。

### Known limitation (recorded, not silently changed)

参考机 `~/.agents/skills` 全部为 symlink → pi 实际加载、omca 观察层按跨 host scope-containment 不变量不跟随；已记入 pack knownUnknowns。放宽 symlink 策略需单独跨 host 决策。

### Merge 记录（owner 本会话授权，2026-09-17）

- #107 squash-merge 为 `e330174`；#108 rebase 到新 main（init.md 单处冲突已解）、修复全部 3 条 Copilot 评论并 resolve 后 squash-merge 为 `168705f`（此处的 #107/#108 均指 wangzitian0/oh-my-code-agent 的 PR）。upstream issue #31（oh-my-code-agent）随 #107 自动关闭。post-merge main 上 4 项检查（build-test/lint/markdown-links/secret-scan）全绿；远端分支已清理。
- 合流前复验：两个 PR 的最终 head 均真机实跑（`omca context`/`report`/`doctor`）+ 全量 `go test -race`（27 packages ok）+ lint 干净 + CI 4/4 绿。

### Follow-ups

- B2（后续 milestone）：基于 `PI_CODING_AGENT_DIR` 的 runtime 隔离（compile/shim/qualify/`omca run pi`）。
- harness 仓库的 `oh-my-code-agent` submodule 指针推进到 `168705f` 由本 PR（infra2#746）完成；后续上游演进仍按 core.harness 边界走 reviewed pin（omca 独立发布）。

## 2026-09-21 pi 主链真实运行实测（owner 指令：pi+glm-5.3-flash E2E 必须消耗 token）

背景：owner 重申架构决策——pi 是唯一主链 host，主链成功=机制成功；机制成功必须以
真实运行（消耗 token）证明；其他 TUI（codex/claude）属覆盖率问题，可图谱化灵活处理。
本次排查确认：此前所有测试均为组件级（unit/fixture/只读观察，#108 的 E2E 是零写入
观察证明），pi 主链从未被真实 E2E 实测，导致下列断链长期无人发现。

### 发现（全部当场实测留证）

- [x] pi+glm-5.3-flash 主链 E2E 通过（真实 token）：`cd /tmp && pi --provider
      zai-coding-cn --model glm-5.3-flash --mode json --no-session -p "Reply with
      exactly: CHAIN-OK"` → 输出 `CHAIN-OK`，usage input 11749 / output 18 /
      reasoning 13 / total 11767 tokens，cost $0.00177，stopReason=stop。
- [x] 裸 `pi -p` 模型解析缺陷：子进程不继承交互会话模型，落到
      amazon-bedrock/claude-opus-4-6 且 403（凭证无效）。交互启动参数未沉淀为可重复
      入口，E2E 必须显式 `--provider/--model`。
- [x] omca pi 适配止步 observation tier（#108 有意为之，B2 runtime 隔离仍是
      follow-up）：`omca run` 仅支持 codex|claude；pi 0.86.1 无 qualified knowledge
      pack（pack 只到 0.85，drift 报 degrade to OBSERVED）；`omca doctor` 报本会话
      pi UNMANAGED、无 compiled generation。
- [x] ~~direnv 授权链路损坏~~ **scout 修正：误报**。direnv v2.37.1 的 `allowed 0`
      是枚举 `Allowed=0`（非布尔 false），allow 文件哈希与当前 .envrc 精确匹配，
      `direnv export bash` 实跑成功；1970 deny watch 是对不存在 deny 文件的常态
      watch（mtime 零值）。真 bug 在 omca doctor：把枚举 int 当布尔解析。
- [x] ws-mem 断链根因精确定位：`ws-mcp-runtime` 渲染器替换表只有 `WS_ROOT` 没有
      `WS_PATH`，config.yml 写 `$WS_PATH` → 运行时引用空值烘焙 →
      `BASIC_MEMORY_HOME=/.ws/basic-memory`；“交互 shell 好 pi 坏”是错觉，
      `direnv export bash` 实跑证明两边拿到同一个坏值。落盘配置未腐蚀，但 CLI 与
      MCP 已双库脑裂。**已修复**：dev_env#22（渲染器加 WS_PATH + ws-mem 兜底
      `! -d` 分支 + 测试），ws-apply 重生成后 ws-mem stats/search 实测恢复。
- [x] ~~skill 双真源漂移~~ **scout 修正：今天 16:14 已被 ws-apply 收敛**。现为三层
      symlink 投影：SSOT=`dev_env/skills/common`（git 仓库，7 skill）→ 汇流点 B
      `~/zitian/.agents/skills`（逐 skill symlink）← A `.ws/.pi/agent/skills`（B
      的别名）；旧 56 个企业技能已迁 `~/.agents/skills.shopee/`。残留风险：
      dev_env 里 audit/SKILL.md 有未提交改动；workspace/config.yml 自引用已随
      dev_env#22 删除；synced/ 是投影体系外的 Claude 云同步失控面。

### Follow-ups（2026-09-21 晚间 5-scout 并行侦察后重排；T4 已落地，T1 PR 待合流）

- [x] T1 固化 pi+glm 主链 E2E smoke 门禁：`tools/pi_chain_smoke.py`
      （不放 omca——pi 不在其 host 列表且违反 submodule 边界）；断言
      message_end(stopReason=stop) + CHAIN-OK + 0<totalTokens≤30000 +
      provider/model 路由；退出码 0/1/2 对齐 omca_gate_policy；挂
      ops-checks.yml 每日 cron `57 3 * * *` + workflow_dispatch 任务
      `pi-chain-smoke`，`# schedule-signal-exempt`（观察性检查永不升格
      blocks_merge）；CI 用 `ZAI_CODING_CN_API_KEY` secret（未配置前 SKIP 绿，
      配置后自动转为真实证明）。本地实跑证据：PASS 9392 tokens / 6.1s。
- [ ] T2 omca pi runtime 隔离（=roadmap M6 交付项）：pi 走 Tier 1 MANAGED
      （PI_CODING_AGENT_DIR 等价 CODEX_HOME，非 claude Tier 2）；8 处 switch +
      测试 ≈700-1000 LOC / 2-3 PR / 2-3 天。前置：本地 checkout detached 在
      #98，落后 origin/main(#111) 3 commits——#108/#109（pi 全部代码）与 ADR
      0006 只在远端/分支，须先同步+合流 tier 分支；PR-B 先做
      PI_CODING_AGENT_DIR 行为学证明（pack knownUnknowns 明言从未验证）。
- [ ] T3 pi 0.86.1 knowledge pack：~0.5-1 天；0.85 pack 本身在
      fix/host-tier-ssot 分支未进 main；0.85→0.86.1 发现面零变化（skills.md
      逐字节相同），capability 可 verbatim 继承；重建二进制后 drift 应转绿。
- [x] T4 环境修复：ws-mcp-runtime 渲染器加 WS_PATH（一行根修）+ ws-apply
      重生成；ws-mem 兜底加 `! -d` 分支消双库脑裂；omca doctor direnv 枚举
      解析修复（omca 侧，待 T2 一起提 PR）。
- [ ] T5 skill 体系收尾：commit dev_env(skills) 的 audit 未提交改动；
      synced/ 失控面是否管控待决策（workspace/config.yml 自引用已随
      dev_env#22 删除）。
