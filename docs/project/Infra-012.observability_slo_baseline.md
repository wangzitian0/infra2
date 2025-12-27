# Infra-012: Observability SLO Baseline

**Status**: Proposed  
**Owner**: Infra

## Goal
Define baseline SLOs, error budgets, and alert thresholds for core services.

## Context
Alerting lacks a consistent SLO model, making it hard to balance noise versus
coverage across platform components.

## Scope
- Identify critical services and SLIs.
- Define SLO targets and alert policies.
- Align with ops.alerting and ops.observability SSOT.

## Deliverables
- SLO registry with owners and thresholds.
- Alert rules update plan.

## Dependencies
- ops.alerting and ops.observability SSOT updates.
- Monitoring data availability.

## PR Links
- TBD

## Change Log
- TBD (create entry when completed)

## Verification
- Alerts fire on synthetic failure tests.
- SLO dashboard reflects real-time budgets.
