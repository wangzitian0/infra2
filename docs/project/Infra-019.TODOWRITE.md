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
- [ ] Add an infra2-sdk-local contributor/agent guide through an independent SDK PR.
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
