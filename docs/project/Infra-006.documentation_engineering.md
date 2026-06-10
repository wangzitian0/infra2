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

## Deliverables
- L0/L1/L2 entry map documented in AGENTS and docs index.
- Link reachability report or checklist for reviews.
- Updated navigation ordering across L1 entry docs.
- `docs/ssot/MANIFEST.yaml` plus governance tests for owner/proof/link drift.

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
