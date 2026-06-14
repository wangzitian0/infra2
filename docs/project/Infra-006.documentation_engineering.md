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

- [x] Define the infra2 SSOT HLS model with 6-8 families, concept boundaries,
  and child binding rules in
  [finance_report#821](https://github.com/wangzitian0/finance_report/issues/821)
  (see [SSOT HLS Family Model](#ssot-hls-family-model) below; documentation
  only — no concept is moved or re-owned in this step).
- [x] Feed infra2 manifest and SSOT files into report-only design metrics for
  family coverage, orphan files, duplicate owners, clause binding,
  proof/checker coverage, and high-risk owner coverage in
  [finance_report#822](https://github.com/wangzitian0/finance_report/issues/822).
- [x] Adopt only incremental and high-risk governance findings as CI gates once
  the metrics baseline is visible in
  [finance_report#823](https://github.com/wangzitian0/finance_report/issues/823).
- [ ] Run threshold-based infra2 SSOT cleanup only after metrics show enough
  evidence for targeted consolidation in
  [finance_report#824](https://github.com/wangzitian0/finance_report/issues/824).

## SSOT HLS Family Model

This is the infra2 high-level structure (HLS) family model defined by
[finance_report#821](https://github.com/wangzitian0/finance_report/issues/821).
It is the **foundation for the
[finance_report#824](https://github.com/wangzitian0/finance_report/issues/824)
threshold cleanup**: it groups the existing infra2 SSOT entries in
[`docs/ssot/MANIFEST.yaml`](../ssot/MANIFEST.yaml) into 6-8 reader-facing
families so cleanup PRs can backfill `family` / `kind` and bind child artifacts
deterministically.

This step is **documentation only**. It does not move, rename, merge, or
re-own any entry; `MANIFEST.yaml` remains the single owner registry. The
family column maps to the `family` field an entry should carry; the member
column lists the current inferred manifest groupings (the
`inferred_family_distribution` keys reported by the finance_report
`tools/report_ssot_governance.py` run over this submodule) that belong to each
family.

### Concept vs clause boundary

- A **concept** is an independently governed SSOT entry with its own owner file
  (`kind` of `concept`, or unset and treated as the default). It is the unit a
  family groups.
- A **clause** is a child inventory, signal table, or matrix that only exists
  to parameterize a parent concept (for example a `*-inventory.yaml` or
  `*-signals.yaml` entry). A clause MUST `parent` its concept and inherit the
  parent's family; it is never reviewed as a standalone concept.
- A **family** is a reader-facing grouping of related concepts. Families do not
  own facts; they route a reader to the owning concept before the individual
  entry. Ownership stays in `MANIFEST.yaml`.

### infra2 family map (6-8 families)

| Family | Scope | Member manifest groups (inferred) |
|---|---|---|
| `core` | Repository architecture, layers, environments, variables, and networks | `core` |
| `bootstrap` | Dokploy/1Password/Vault bootstrap, vars and secrets, DNS/cert, and IaC runner | `bootstrap` |
| `platform` | Domain/routing, SSO, deployer automation, AI provider, and analytics | `platform` |
| `db` | Platform/business PostgreSQL, Redis, ClickHouse, ArangoDB, and Vault DB integration | `db` |
| `vault` | Vault app-token self-refresh inventory and audit | `vault` |
| `ops` | CI/CD pipeline, E2E regression, recovery, storage/backup, observability, alerting, and availability ledger | `ops` |
| `watchdog` | Active watchdog signal inventory and ownership | `watchdog` |

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
| 2026-06-15 | Defined the infra2 SSOT HLS family model (6-8 families + concept/clause boundary) for finance_report#821 |

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
