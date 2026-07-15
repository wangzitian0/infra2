# Infra-017: TrueAlpha Dagster Capture Runtime

**Status**: In Progress
**Owner**: Infra
**Priority**: P1
**Branch**: `feat/truealpha-dagster-capture`

## Goal

Deploy one digest-pinned, environment-isolated Dagster runtime that can prove the
bounded TOPT Staging capture before a separately approved Production expansion.

## Context

TrueAlpha already has isolated Staging Postgres/S3 and a host-only moomoo OpenD, but
real ingestion still runs as manual host scripts. That cannot prove scheduler authority,
persistent metadata, exact artifact promotion, retry behavior, or row-complete manifests.
OpenD's loopback binding also prevents a normal overlay-network container from reaching it.

## Scope

- [x] Define the host-network boundary without publishing OpenD or Dagster UI.
- [x] Add dedicated Vault, data directory, resource limits, and environment ports.
- [x] Require one OCI digest/release/config binding for webserver and daemon.
- [x] Add runtime image verification and machine-readable governance entries.
- [ ] Publish the accepted data-engine digest and configure Staging Vault fields.
- [ ] Deploy Staging and prove persistent daemon/UI/runtime checks.
- [ ] Attach two scheduled TOPT cycles, retry, and changed-vintage evidence.
- [ ] Introduce Production only after its application-side release/scope is approved.

## Deliverables

- `truealpha/truealpha/20.data_engine/` deployment unit.
- `core.truealpha_runtime` SSOT and audit inventory coverage.
- Staging run IDs, image/release/config IDs, manifests, and verification commands.

## PR Links

- None yet.

## Change Log

| Date | Change |
|---|---|
| 2026-07-12 | Initialized the isolated digest-pinned Dagster deployment contract. |

## Verification

- [ ] `uv run pytest libs/tests/test_truealpha_data_engine.py`
- [ ] `uv run pytest libs/tests/test_vault_self_refresh_audit.py libs/tests/test_backup_verification.py`
- [ ] `docker compose config` succeeds with synthetic non-secret coordinates.
- [ ] Staging runtime image, daemon heartbeat, schema placement, and loopback boundaries pass.

## References

- [SSOT: core.truealpha_runtime](../ssot/core.truealpha_runtime.md)
- TrueAlpha issues #27, #51, #53, #67, and #68
