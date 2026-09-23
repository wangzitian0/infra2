# Infra-020: TrueAlpha Production DataHub

**Status**: In Progress — narrow production slice evidenced; prerequisite reconciliation open
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
- [x] Implement and prove the manual Production capture and read-only query path.
- [x] Record deployment and query evidence without enabling a scheduler.

TrueAlpha [#271](https://github.com/wangzitian0/truealpha/issues/271) closed on
2026-07-23 after a real manual Production run and `mart.current_pointer` advance.
The closing evidence names run `capture-run:f8f2232b704b90abd2b4502d146b6a6214a54f0ded128b375ac76daa3ab05ea6`,
20 core results, 84 terminal outcomes, and the App/MCP reading the same exact
run. [#475](https://github.com/wangzitian0/truealpha/issues/475) and
[#426](https://github.com/wangzitian0/truealpha/issues/426) add authenticated
Production page and MCP evidence. The issue states no recurring schedule was
enabled and the broader Production graduation remains out of scope.

This delivery **does not prove that the original Infra-020 prerequisites were
accepted first**. As of 2026-09-23, TrueAlpha #171 (Staging capture), #205 (D5),
#41 (stable read), #52 (release/recovery), #60 (source rights), and #61 (SLO)
remain open. The project therefore stays in progress until the owner either
documents a scoped exception to that sequence or reconciles those dependencies
with their evidence. Do not silently tick them merely because #271 closed.

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
| 2026-09-23 | Reconciled the stale tracker with TrueAlpha #271/#475/#426: real manual Production capture and downstream reads are evidenced, while six originally required prerequisite issues remain open. Infra-020 stays in progress pending explicit dependency/exception reconciliation. |
| 2026-07-16 | Registered the narrow Production TOPT DataHub delivery scope as TrueAlpha issue #271. |

## Verification

- [ ] From the `repos/truealpha` checkout, `uv run python tools/check_delivery_governance.py`
      accepts the queued batch.
- [x] The manual Production command records all 84 TOPT obligations without a schedule (TrueAlpha #271 closing evidence).
- [ ] Read-only status and trace queries reconcile with append-only persisted evidence. TrueAlpha #271/#426/#475 prove same-run App/MCP consumption; this tracker still needs the complete status/trace and negative authorization check against the original acceptance wording.
- [ ] Downstream credentials cannot read `raw`/`staging` or return raw bytes.

## References

- TrueAlpha issue #271: Manual Production TOPT DataHub capture and reads.
- TrueAlpha issues #41, #52, #53, #60, #61, #66, #171, #205, #207, and #210.
- [SSOT: TrueAlpha Runtime](../ssot/core.truealpha_runtime.md)
