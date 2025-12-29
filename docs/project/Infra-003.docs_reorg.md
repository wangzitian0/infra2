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
- None yet.

## Change Log
- 2025-12-28: Initialized docs reorg and Pages site.
- 2025-12-28: Fixed Top30 doc issues and normalized links/anchors.
- 2025-12-29: Aligned SSOT/onboarding/platform docs with Python + Dokploy deployment.
- 2025-12-29: Refactored E2E regression tests to match Dokploy/Vault/Authentik stack.
- 2025-12-29: Aligned env rules (docs + tooling) with new three-tier scheme.
- 2025-12-30: Fixed MkDocs external link warnings and aligned docs entry links.
- 2025-12-30: Updated root Quick Start to match 1Password bootstrap flow.
- 2025-12-30: Aligned bootstrap env seed notes with local.bootstrap behavior.

## Verification
- `mkdocs build --config-file docs/mkdocs.yml` passes without warnings.
