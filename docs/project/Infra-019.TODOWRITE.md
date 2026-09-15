# Infra-019: TODOWRITE (Workspace Harness Control Plane)

**Status**: Active
**Last Updated**: 2026-09-15

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

The integrated development pins are SDK `3fecc8e`, OMCA `fed8928`, TrueAlpha `a65e373`,
and Finance Report `39e8ecf0`. Each full commit is identified above. The parent integration
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
