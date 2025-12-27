# Infra-015: Data Platform Cost Optimization

**Status**: Proposed  
**Owner**: Infra

## Goal
Reduce storage and compute costs across data services without sacrificing
reliability or performance.

## Context
Data services (PostgreSQL, ClickHouse, Redis, ArangoDB) grow in capacity and cost
with limited retention and sizing governance.

## Scope
- Audit current storage and retention settings.
- Define tiered storage and retention policies.
- Propose sizing changes with performance impact analysis.

## Deliverables
- Cost baseline report and optimization plan.
- Updated retention policies in SSOT.

## Dependencies
- ops.storage SSOT alignment.
- Metrics from observability stack.

## PR Links
- TBD

## Change Log
- TBD (create entry when completed)

## Verification
- Cost delta tracked month over month.
- No regression in performance SLIs.
