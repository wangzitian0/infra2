# Infra-006: Pipeline V2 Upgrade

**Status**: Archived  
**Owner**: Infra

## Summary
Upgrade the CI/CD pipeline to reconcile cross-workflow state using SHA-to-PR
reverse lookup, improving PR status table consistency and auditability.

## Scope
- Align workflow_run events with PR context.
- Stabilize multi-commit status reporting.
- Harden pipeline feedback for plan/apply and infra-flash comments.

## PR Links
- PR #289: https://github.com/wangzitian0/infra/pull/289
- PR #290: https://github.com/wangzitian0/infra/pull/290

## Change Log
- [2025-12-19: Pipeline V2 Upgrade](../../change_log/2025-12-19.pipeline_v2_upgrade.md)

## Git Commits (Backtrace)
- ac56920 Final Pipeline Validation (#290)
