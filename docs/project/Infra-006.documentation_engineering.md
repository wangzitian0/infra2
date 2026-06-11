# Infra-006: Documentation Engineering

**Status**: In Progress  
**Owner**: Infra  
**Priority**: P2  
**Branch**: `feat/ssot-bootstrap-pitfalls`

## Goal
Establish a repeatable documentation engineering workflow with measurable link reachability and machine-readable SSOT ownership.

## Context
The wiki has multiple entry points and deep references; we need consistent navigation, reachability, and maintenance checks.

## Scope
- [x] Define L0/L1/L2 entry map and update cross-links.
- [x] Add a lightweight link reachability check (script or checklist).
- [x] Normalize entry ordering across key docs for discoverability.
- [x] Add a machine-readable SSOT manifest and tests that keep the SSOT index, owner files, proof anchors, and project SSOT links aligned.
- [ ] Track SSOT HLS governance as design -> metrics -> gradual gates -> threshold cleanup.

## Deliverables
- L0/L1/L2 entry map documented in AGENTS and docs index.
- Link reachability report or checklist for reviews.
- Updated navigation ordering across L1 entry docs.
- `docs/ssot/MANIFEST.yaml` plus governance tests for owner/proof/link drift.
- HLS governance checklist with GitHub issue links for incremental SSOT convergence.

## SSOT HLS Governance Loop

This high-level structure (HLS) roadmap is a design roadmap, not a proof source.
GitHub issues, generated reports, and CI gates own live status.

As-is:

- The infra2 SSOT manifest and reachability tests protect owner/proof/link
  drift, but the family/concept/clause design model is still implicit.
- Platform, database, ops, and bootstrap entries can expose child operational
  facts as independently governed concepts when they should bind to a parent
  authority surface.
- FR application semantics and infra2 platform semantics are related, but the
  cross-system authority boundary is not measured.

To-be:

- [ ] Define the infra2 SSOT HLS model with 6-8 families, concept boundaries,
  and child binding rules in
  [finance_report#821](https://github.com/wangzitian0/finance_report/issues/821).
- [ ] Feed infra2 manifest and SSOT files into report-only design metrics for
  family coverage, orphan files, duplicate owners, clause binding,
  proof/checker coverage, and high-risk owner coverage in
  [finance_report#822](https://github.com/wangzitian0/finance_report/issues/822).
- [ ] Adopt only incremental and high-risk governance findings as CI gates once
  the metrics baseline is visible in
  [finance_report#823](https://github.com/wangzitian0/finance_report/issues/823).
- [ ] Run threshold-based infra2 SSOT cleanup only after metrics show enough
  evidence for targeted consolidation in
  [finance_report#824](https://github.com/wangzitian0/finance_report/issues/824).

## PR Links
- None yet.

## Change Log
| Date | Change |
|------|--------|
| 2025-12-31 | Initialized project |
| 2025-12-31 | Standardized L1 entry ordering and labels across docs |
| 2025-12-31 | Added reachability report and PageRank findings |
| 2025-12-31 | Linked Bootstrap/E2E subdocs and closed reachability gaps |
| 2025-12-31 | Added TODOWRITE links for active projects and refreshed metrics |
| 2026-06-10 | Added SSOT manifest governance for owner files, proof anchors, README index parity, and project SSOT link reachability |
| 2026-06-11 | Added incremental SSOT HLS governance loop with finance_report issue references |

## Verification
- [ ] L0 to L2 docs are reachable in <= 2 hops.
- [ ] Link check passes for local docs.
- [x] SSOT manifest governance test passes.

## TODOWRITE
- [Infra-006.TODOWRITE.md](./Infra-006.TODOWRITE.md)

## References
- [文档索引](../README.md)
- [SSOT Index](../ssot/README.md)
- [SSOT: ops.standards](../ssot/ops.standards.md)
