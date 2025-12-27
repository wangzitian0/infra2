# Infra-014: Terraform Module Hardening

**Status**: Proposed  
**Owner**: Infra

## Goal
Standardize module interfaces and defensive checks to reduce drift, import
friction, and apply-time failures.

## Context
Modules vary in outputs, preconditions, and import readiness, which complicates
operations and onboarding.

## Scope
- Define module interface conventions.
- Add preconditions and outputs for auditability.
- Document import strategies per module.

## Deliverables
- Module checklist and refactor plan.
- Updated module docs and examples.

## Dependencies
- ops.standards defensive maintenance rules.
- Module owners' approval.

## PR Links
- TBD

## Change Log
- TBD (create entry when completed)

## Verification
- Plan shows whitebox outputs for dynamic IDs.
- Import docs tested in a sandbox environment.
