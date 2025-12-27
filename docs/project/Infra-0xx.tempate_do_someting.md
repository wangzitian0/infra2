# Infra-011: Platform Service Catalog

**Status**: Proposed  
**Owner**: Infra

## Goal
Create a single source catalog of platform services, endpoints, ownership, and
access patterns for operators and developers.

## Context
Service entry points are spread across README files and tribal knowledge. A
catalog reduces onboarding time and incident triage latency.

## Scope
- Inventory platform and data services with URLs and auth methods.
- Map owners and escalation paths.
- Link to SSOT and operational runbooks.

## Deliverables
- Catalog page in docs with service metadata.
- Automation hook to validate link freshness.

## Dependencies
- docs/ssot index alignment.
- Portal/SSO endpoint inventory.

## PR Links
- TBD

## Change Log
- TBD (create entry when completed)

## Verification
- All listed endpoints reachable in staging.
- Links resolve to SSOT or runbooks.
