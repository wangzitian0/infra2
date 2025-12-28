# Infra-003: Documentation Reorganization

**Status**: In Progress  
**Owner**: Infra  
**Priority**: P1

## Goal
Consolidate documentation into a clean, navigable structure with a stable SSOT and MkDocs site.

## Context
Docs are scattered across multiple paths and contain stale links/anchors. We need a single, consistent entry point and a clean docs site build.

## Scope
- Normalize docs structure and project catalog.
- Fix broken links/anchors across markdown files.
- Maintain MkDocs site config and Pages workflow.

## Deliverables
- Clean docs navigation and project index.
- Reduced doc lint/link warnings.
- MkDocs site builds without warnings.

## PR Links
- TBD

## Change Log
- 2025-12-28: Initialized docs reorg and Pages site.

## Verification
- `mkdocs build --config-file docs/mkdocs.yml` passes without warnings.
