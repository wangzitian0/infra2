# Infra-015: deploy_v2 — the unified, trustworthy deploy front door

**Status**: Done
**Owner**: Infra
**Priority**: P0
**Root issue**: finance_report#1072 (commit-addressed deploy primitive & delivery pipeline)

## Goal
One front door for every deploy in infra2 — app and platform, every environment —
addressed by a single coordinate and routed to the right backend, with the gates
(prod data lane, staging-first, prod-only, vault/config parity) enforced in one
place instead of scattered across bash and per-workflow YAML. After this EPIC,
`deploy-platform.yml` no longer deploys services; `deploy_v2` is the sole path.

## The coordinate
```
(service, type, version_ref, iac_ref)
```
- **service** — full registry id, e.g. `finance_report/app`, `platform/redis`.
- **type** — the discriminant: one of `staging`, `prod`, `preview/branch`,
  `preview/pr`, `preview/commit`, `preview/tag`, `canary`. `env` and `sub_domain`
  are **derived** from it; `accepted_forms` per type fails closed (e.g. `prod`
  accepts only release forms).
- **version_ref** — polymorphic app code selector (PR# / sha / tag / branch /
  release). For `iac_pinned` services it **degenerates**: the artifact is the
  `iac_ref`-pinned stack, so code identity == the infra2 sha (see §4.7.2 proof).
- **iac_ref** — pins *how* it is deployed (the infra2 stack revision).

The four axes are orthogonal; completeness is proven in
[SSOT core.environments §4.7.2](../ssot/core.environments.md).

## Backends (routing by service class)
| Service class | Backend | Notes |
|---------------|---------|-------|
| app, fixed env | `deploy_primitive` | staging / prod |
| app, preview | `preview_lifecycle` | the `preview/*` types |
| **`iac_pinned`** (all non-app) | **`iac_runner` `/deploy` webhook** | via `libs/iac_runner_client`, HMAC-SHA256 signed; `version_ref` unused |

`iac_pinned` is **derived** from `libs/service_registry` (Infra-013), never a
hand-copied list — adding a platform service cannot drift the deploy registry.

## Trigger model (SSOT §4.6 — the corrected model)
| Target | Trigger |
|--------|---------|
| **report-branch-main** | **auto** — app `main` push → `repository_dispatch` → `deploy-report-main.yml` |
| staging / prod (app **and** platform) | **manual + pinned release tag** (staging pins the SAME tag as prod for parity; platform pins the `iac_ref` tag) |

`deploy-platform.yml` is now **bootstrap-only**: it updates the iac_runner
container itself when `bootstrap/06.iac_runner/**` changes — a lifecycle
`deploy_v2` cannot own (it depends on the runner being up). It no longer
auto-deploys platform services on push.

## Scope (MECE, delivered as merged PRs)
- [x] **Coordinate** — converged `(service, type, version_ref, iac_ref)` (#354);
  drift sync + prod-parity gap closed (#356).
- [x] **Canary as a first-class probe** — fast-fail + resilient teardown + 5xx
  retries (#355); post-merge CI run on deploy-tooling changes (#357); PR gate +
  failure-domain classification + out-of-band Feishu alert (#358).
- [x] **Completeness proof** — coordinate proven complete, §4.7.2 (#359).
- [x] **Platform backend** — `iac_pinned` services route to the signed iac_runner
  webhook; redis first (#361); deploys **wait + gate** on terminal iac_runner
  status (#365).
- [x] **No drift** — platform registry derived from `service_registry`
  (Infra-013), not hand-copied (#363); sync_runner service/task lists derived
  from `discover_services()` (#371).
- [x] **Trigger model SSOT fix** — §4.6 env→trigger model corrected (#366); it was
  the stale section that originally misled the design.
- [x] **Cutover** — `deploy-platform.yml` reduced to iac_runner bootstrap;
  `deploy_v2` is the sole platform path; report-branch-main auto **receiver**
  added (#371, closes #370).
- [x] **report-branch-main sender** — app `main` push dispatches into infra2
  (finance_report#1173) — the cross-repo half of the auto target.

## Out of scope
- Re-implementing `Deployer.sync` inside deploy_v2: the platform backend triggers
  the SAME signed iac_runner webhook the old path used (byte-for-byte fidelity),
  rather than re-deriving Context-coupled sync logic.
- Migrating every one of the 12 platform services by hand: routing is identical
  across them; the path is verified across service classes (below), not per-service.

## Verification ("The Proof")
- [x] `pytest libs/tests/test_iac_runner_deploy_result.py` — signed webhook
  formula, wait/gate on terminal status, cutover assertions (old "Trigger IaC
  Runner" step + `platform/**` push paths gone).
- [x] `pytest libs/tests/test_service_registry.py` — sync_runner service set is
  derived, not hand-listed (AST drift guard).
- [x] **Live, post-cutover on `main`** — verified across classes:
  - backing store: `platform/redis` (#361), `platform/postgres` (wait/gate
    confirmed live: `iac_runner_final` `completed` / `succeeded:1`).
  - web-facing: `platform/authentik` → `succeeded:1`.
  - prod-only gate: `platform/signoz` → correctly **rejected** on staging
    ("prod-only; cannot deploy to staging").

## References
- [SSOT: core.environments §4.6 / §4.7.2](../ssot/core.environments.md)
- [SSOT: ops.pipeline](../ssot/ops.pipeline.md)
- `libs/iac_runner_client.py`, `tools/deploy_v2.py`, `tools/deploy_contract.py`
- Root: finance_report#1072 · cutover issue #370
- [[Infra-013]] service registry SSOT (the registry deploy_v2 derives from)

## Change Log
| Date | Change |
|------|--------|
| 2026-06-17 | EPIC recorded as Done: coordinate + platform backend + cutover + both halves of the report-branch-main auto target shipped and live-verified. |
