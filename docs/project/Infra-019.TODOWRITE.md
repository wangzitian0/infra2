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
      (implementation and checks complete; integration awaits owner review).
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
  Independent change #820 adopts the existing released enum/parser through its public
  compatibility import path; dependency instances remain app-owned.
- [x] Finance SDK wheel coordinates were hand-maintained in three workflows.
  Independent #2004 change reads the generated backend lock and checks its agreement
  with the dependency declaration; checksum enforcement stays at acquisition.
- [x] SDK silently ignored missing/nonmapping named override tables. Independent SDK
  patch rejects them before writing a manifest, with failing-first regression tests.
- [ ] Merge the independent reviewed changes and update the integrated snapshot after
  their applicable checks pass. No new SDK release or production deployment is implied.

### OMCA evidence and limitations

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

### Review delivery and proof

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
- Codex 0.153.4 rejected the old generated approval setting. #93 adopts the documented
  untrusted-project migration with the existing read-only sandbox default, and versions
  both bootstrap/full-generation cache identities. Real safe introspection now proves
  OMCA MCP inclusion, repository Skill inclusion, native sentinel exclusion, and unchanged
  native configuration snapshots. Claude 2.1.267 proves MCP inclusion/exclusion. All Go
  race tests and lint pass; coverage is 80.4%. Human TUI/restart/model proof and Claude
  Skill inventory remain UNKNOWN; this does not establish full interactive MVP acceptance.
- TrueAlpha #821 was rebased onto main including the independently delivered MinIO
  mirror fix #825. All applicable checks passed at `50bee8a`; a subsequent review nit
  is being fixed and requires another current-head CI/review pass.
- Finance #2036 now also restores both MinIO server/client acquisition through immutable
  upstream Quay artifacts under #2037. Its existing toolchain gate now covers each CI
  acquisition job and preview Compose. Six drift mutations failed against the old guard;
  all 14 toolchain tests and static preflight pass after the repair. Remote CI must prove
  actual image acquisition because Docker is unavailable in the local workspace.
- The SDK gets a standalone app readiness example with an installed-wheel HTTP smoke:
  healthy, unhealthy, and omitted required dependency cases exercise the documented
  public entrypoint. Dependency names, required tiers, and business policy remain app-owned.

Remaining: finish current-head review/checks, merge eligible PRs, verify post-merge runs,
and update parent snapshots only to reviewed main commits. Human-only host evidence
must stay visibly incomplete until actually supplied. No production promotion is planned.
