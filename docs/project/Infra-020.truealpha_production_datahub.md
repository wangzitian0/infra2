# Infra-020: TrueAlpha Production DataHub

**Status**: In Progress
**Owner**: Infra
**Priority**: P1

## Goal

Manually run the immutable TOPT capture in isolated Production and make its bounded,
append-only capture metadata available to downstream read-only queries without enabling
a recurring schedule or claiming the full Production shadow release.

## Context

TrueAlpha's existing DataHub work proves only contracts or bounded Staging capture. The
requested narrow delivery needs an auditable Production handoff from capture through
raw/normalized persistence to downstream status and provenance reads. It is intentionally
smaller than TrueAlpha issue #53, which remains responsible for the seven-module
Production shadow canary.

## Scope

- [x] Register TrueAlpha issue #271 and a queued batch for the narrow acceptance.
- [ ] Obtain a D4 handoff that explicitly authorizes the manual Production consumer.
- [ ] Consume accepted D5/D6, TOPT Staging, stable read, release/recovery, rights, and
      SLO prerequisites.
- [ ] Implement and prove the manual Production capture and read-only query path.
- [ ] Record deployment and query evidence without enabling a scheduler.

## Deliverables

- `governance/batches/D8-manual-production-topt-datahub.v1.json` in TrueAlpha.
- Explicit Production prerequisites, handoff identities, and rollback boundary.
- Operator-triggered TOPT capture evidence and downstream read-only query evidence.

## PR Links

- TrueAlpha #272: register the queued D8 batch.
- infra2 #497: track this project and its non-bypassable dependencies.

## Change Log

| Date | Change |
|------|--------|
| 2026-07-16 | Registered the narrow Production TOPT DataHub delivery scope as TrueAlpha issue #271. |

## Verification

- [ ] From the `repos/truealpha` checkout, `uv run python tools/check_delivery_governance.py`
      accepts the queued batch.
- [ ] The manual Production command records all 84 TOPT obligations without a schedule.
- [ ] Read-only status and trace queries reconcile with append-only persisted evidence.
- [ ] Downstream credentials cannot read `raw`/`staging` or return raw bytes.

## References

- TrueAlpha issue #271: Manual Production TOPT DataHub capture and reads.
- TrueAlpha issues #41, #52, #53, #60, #61, #66, #171, #205, #207, and #210.
- [SSOT: TrueAlpha Runtime](../ssot/core.truealpha_runtime.md)
