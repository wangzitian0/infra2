# Infra-013: Backup and Restore Game Day

**Status**: Proposed  
**Owner**: Infra

## Goal
Prove backup integrity by running scheduled restore drills for critical data
stores and documenting recovery time metrics.

## Context
Backups exist but restore confidence is untested. A game day validates recovery
procedures before incidents occur.

## Scope
- Select critical databases and Vault.
- Define restore runbooks and automation hooks.
- Capture RTO/RPO metrics and gaps.

## Deliverables
- Restore drill checklist and schedule.
- Evidence artifacts (logs, timestamps).

## Dependencies
- ops.recovery SSOT alignment.
- Access to backup storage credentials.

## PR Links
- TBD

## Change Log
- TBD (create entry when completed)

## Verification
- Successful restore in staging.
- Documented RTO/RPO vs targets.
