# Infra-020: TODOWRITE (TrueAlpha Production DataHub)

**Status**: Active
**Owner**: Infra

## Purpose

Track the non-bypassable prerequisites and evidence for the manual Production TOPT
DataHub slice.

## Top Issues (Top 30)

- [ ] 2026-09-23 evidence reconciliation: [truealpha#271](https://github.com/wangzitian0/truealpha/issues/271), [#426](https://github.com/wangzitian0/truealpha/issues/426) and [#475](https://github.com/wangzitian0/truealpha/issues/475) closed with real Production manual capture, `mart.current_pointer`, and matching App/MCP run identity. Original prerequisite issues [#171](https://github.com/wangzitian0/truealpha/issues/171), [#205](https://github.com/wangzitian0/truealpha/issues/205), [#41](https://github.com/wangzitian0/truealpha/issues/41), [#52](https://github.com/wangzitian0/truealpha/issues/52), [#60](https://github.com/wangzitian0/truealpha/issues/60), [#61](https://github.com/wangzitian0/truealpha/issues/61) are still open. Keep their checkboxes open; obtain an owner-scoped sequence exception or reconcile each prerequisite against the real run before marking Infra-020 complete.
- [ ] TrueAlpha D4: publish an accepted DataHub handoff that explicitly authorizes
      the [truealpha#271](https://github.com/wangzitian0/truealpha/issues/271) Production consumer; the current scopes allow only Local/CI consumers
      or D3 in Staging.
- [ ] [TrueAlpha #205](https://github.com/wangzitian0/truealpha/issues/205) / D5: accept the append-only capture-control handoff; do not
      duplicate its migration or control-plane paths while [TrueAlpha PR #266](https://github.com/wangzitian0/truealpha/pull/266) is active.
- [ ] [TrueAlpha #207](https://github.com/wangzitian0/truealpha/issues/207) / D6: publish accepted confidence and indexed-provenance
      persistence before trusted metadata and trace claims.
- [ ] [TrueAlpha #171](https://github.com/wangzitian0/truealpha/issues/171): prove the bounded TOPT Staging capture before Production source
      activation.
- [ ] [TrueAlpha #41](https://github.com/wangzitian0/truealpha/issues/41): publish the stable DataHub read handoff for downstream queries.
- [ ] [TrueAlpha #52](https://github.com/wangzitian0/truealpha/issues/52), [#60](https://github.com/wangzitian0/truealpha/issues/60), and [#61](https://github.com/wangzitian0/truealpha/issues/61): approve exact release/recovery, source rights/budget,
      and frozen applicability/SLO policy before any Production source call.
- [ ] Production: populate only environment-scoped Vault credentials and preserve
      independent Postgres, bucket, ledger, and Dagster metadata boundaries.
- [ ] Production: attach manual command, run/scope/manifest IDs, query reconciliation,
      negative authorization checks, and rollback evidence.
