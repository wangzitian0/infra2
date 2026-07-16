# Infra-020: TODOWRITE (TrueAlpha Production DataHub)

**Status**: Active
**Owner**: Infra

## Purpose

Track the non-bypassable prerequisites and evidence for the manual Production TOPT
DataHub slice.

## Top Issues (Top 30)

- [ ] TrueAlpha D4: publish an accepted DataHub handoff that explicitly authorizes
      the #271 Production consumer; the current scopes allow only Local/CI consumers
      or D3 in Staging.
- [ ] TrueAlpha #205 / D5: accept the append-only capture-control handoff; do not
      duplicate its migration or control-plane paths while PR #266 is active.
- [ ] TrueAlpha #207 / D6: publish accepted confidence and indexed-provenance
      persistence before trusted metadata and trace claims.
- [ ] TrueAlpha #171: prove the bounded TOPT Staging capture before Production source
      activation.
- [ ] TrueAlpha #41: publish the stable DataHub read handoff for downstream queries.
- [ ] TrueAlpha #52, #60, and #61: approve exact release/recovery, source rights/budget,
      and frozen applicability/SLO policy before any Production source call.
- [ ] Production: populate only environment-scoped Vault credentials and preserve
      independent Postgres, bucket, ledger, and Dagster metadata boundaries.
- [ ] Production: attach manual command, run/scope/manifest IDs, query reconciliation,
      negative authorization checks, and rollback evidence.
