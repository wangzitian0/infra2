# Infra-010: Bootstrap Drift Reconcile

**Status**: Archived  
**Owner**: Infra

## Summary
Reconcile post-merge bootstrap drift by adjusting CI behavior, adopting existing
resources before apply, and updating health checks.

## Scope
- Run bootstrap drift reconciliation on main pushes.
- Import existing Helm releases and secrets before apply.
- Move DNS/HTTPS checks to post-apply verification.

## PR Links
- PR #416: https://github.com/wangzitian0/infra/pull/416

## Change Log
- [2025-12-25: Reconcile Bootstrap Drift in Post-Merge CI](../../change_log/2025-12-25.bootstrap_drift_reconcile.md)

## Git Commits (Backtrace)
- cde8257 fix(bootstrap): reconcile post-merge drift (#416)
