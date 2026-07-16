# Infra-019: TODOWRITE (Workspace Harness Control Plane)

**Status**: Active
**Last Updated**: 2026-07-16

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

- [ ] Add read-only `harness status` for pinned checkout, remote head, and release identity.
- [ ] Add an infra2-sdk-local contributor/agent guide through an independent SDK PR.
- [ ] Decide whether workspace preference changes need their own version identifier.
- [ ] Add cross-repository compatibility matrix reporting from released evidence.
- [ ] Archive Infra-019 after the selected follow-ups are complete or explicitly deferred.

> App policy adoption is intentionally not a TODO. Finance Report and TrueAlpha remain
> autonomous and may independently reuse a preference if their maintainers choose.
