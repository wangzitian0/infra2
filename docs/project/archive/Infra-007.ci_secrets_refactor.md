# Infra-007: CI Secrets Refactor

**Status**: Archived  
**Owner**: Infra

## Summary
Refactor the CI secrets chain to enforce DRY loading, integrity guards, and a
single source for bootstrap secret handling.

## Scope
- Consolidate secrets loader paths.
- Add validation guardrails for CI secret flow.
- Reduce duplicate logic across workflows.

## PR Links
- PR #278: https://github.com/wangzitian0/infra/pull/278
- PR #402: https://github.com/wangzitian0/infra/pull/402

## Change Log
- [2025-12-19: CI/CD Secret Chain Refactoring](../../change_log/2025-12-19.ci_secrets_refactor.md)

## Git Commits (Backtrace)
- 6b49413 Refactor: Harden Bootstrap & Tools Migration (#402)
- cfb7a57 chore: finalize secret chain with integrity guards (#278)
