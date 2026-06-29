# Infra-016: CI Gate Inventory — coordinate-ize infra CI, and de-overlap app vs infra responsibilities

## Goal

Make infra CI **reviewable at a glance the way `deploy_v2` is** — a small set of
orthogonal coordinates instead of 23 hand-wired jobs — and draw a **non-overlapping
responsibility line** between the app repo (`finance_report`) and the infra repo
(`infra2`) for CI/CD governance.

`deploy_v2` is reviewable because every deploy is one coordinate row
`(service, type, version_ref, iac_ref)`. The app repo already did the CI equivalent:
`docs/ssot/ci-gate-inventory.yaml` expresses every gate as `(stage, task_category) →
workflow:job + metadata`. Infra has **no such coordinate inventory** — its CI is still
"read the workflow to learn what's covered". This EPIC closes that gap and fixes the
asymmetry, with one hard rule: **app and infra must not own the same thing twice.**

## Context

Snapshot (both repos, latest `main`): **18 workflows · 61 jobs · 373 steps.**

| Asset | app (`finance_report`) | infra (`infra2`) |
|---|---|---|
| Narrative CI SSOT | `docs/ssot/ci-cd.md` (Env×Stage matrix, path-risk→gate, PR-vs-main) | `docs/ssot/ops.pipeline.md` (deploy/release trigger model) |
| **Gate coordinate inventory** | ✅ `ci-gate-inventory.yaml` — 38 gates, 8 stages × 20 task_categories | ❌ none |
| Test→stage matrix | ✅ `test-execution-matrix.yaml` | ❌ none |
| Delivery gates | ✅ `delivery-gates.yaml` | ❌ none |
| Env×Stage **result** contract | (app CI emits) | ✅ `libs/pipeline_stage_contract.py` (§5.4) |
| Fail-closed audit (inventory↔workflow) | ❌ none | ❌ none |

The app gate schema is already richer than `deploy_v2`'s four axes — each gate carries
`stage, task_category, workflow, job, owner, triggers, required_by_finish,
blocks_workflow, inputs, outputs, artifacts, failure_semantics, action`. The analogy is
exact: `deploy_v2 (service, type)` ↔ `CI (task_category, stage)`. The work is **not to
invent a model** — it exists — but to (a) give infra a symmetric inventory, (b) make the
shared parts single-owner so the two repos don't drift or overlap, and (c) add the
missing drift audit to *both* sides.

## Target state

### 1. Responsibility line — *contract* vs *instance* (the de-overlap rule)

The single principle that prevents overlap:

> **Contracts (schema + shared vocabulary) have exactly one owner — infra. Gate
> *instances* are owned by whoever runs them, and no gate is listed in both inventories.**

| Asset | Single owner | The other side |
|---|---|---|
| Delivery **stage vocabulary** (`local.* / github_ci.* / preview.* / staging.* / prod.* / ops.* / manual.*`) | **infra** — promoted into `docs/ssot/delivery-stages.yaml` | app *references* it |
| Gate inventory **schema** (the gate object's fields) | **infra** | app *conforms* |
| **Env×Stage result** contract (`pipeline_stage_contract`) | **infra** (already) | app *emits* into it |
| **`deploy_v2` coordinate** (`service, type, version_ref, iac_ref`) | **infra** (already, §4) | app *calls* |
| **app gate instances** (backend / frontend / AC / coverage gates) | **app** (`ci-gate-inventory.yaml`) | infra never lists them |
| **infra gate instances** (compose / vault / deployer / reconcile / preview-leak) | **infra** (new `ci-gate-inventory.yaml`) | app never lists them |
| **`task_category` vocabulary** | each owns its own subset (app: `backend_*/frontend_*`; infra: `compose_*/vault_*`) | shared *namespace*, disjoint values |

Three non-overlap invariants (enforced by audit, §3):

1. **Every gate appears in exactly one inventory.** An app gate never appears in infra's
   inventory, and vice versa. No "transitional duplicate" survives across the line.
2. **The `stage` axis is the only shared vocabulary, and it is single-owner (infra).**
   Both inventories reference the same `delivery-stages.yaml`; neither redefines stages
   locally. (Today app embeds its own `stages:` block — that moves to infra and app
   references it.)
3. **Cross-repo views are produced by joining on the shared stage axis, never by
   copying.** The full delivery-chain matrix is a *read-time join* of the two
   inventories, not a third hand-maintained list.

Why infra owns the contracts: `infra2` is the delivery platform/artifact — it defines the
*shape* of the pipeline (stages, deploy coordinate, result schema). The app is a consumer
that fills in its own gates. This mirrors the established **App emits artifact / Infra
consumes** boundary in `AGENTS.md`; here the same arrow means *infra defines the pipeline
contract, app declares its gates inside it*.

### 2. Infra2 deliverables

| ID | Deliverable | Notes |
|----|-------------|-------|
| **D1** | `docs/ssot/delivery-stages.yaml` — authoritative stage vocabulary (single owner). | App's `ci-gate-inventory.yaml` drops its embedded `stages:` block and references this. Cross-repo PR pair. |
| **D2** | `docs/ssot/ci-gate-inventory.yaml` (infra) — coordinate-ize the ~23 CI/validation jobs (`infra-ci` 7, `ops-checks` 6, reconcile dry-run gate, apply-observability, drift reports). Same schema as app. | infra `task_category` values: `compose_validate, vault_policy, vault_agent, deployer_contract, secret_preflight, op_healthcheck, reconcile_plan, preview_leak, drift_report, …` |
| **D3** | `tools/ci_gate_audit.py` — **fail-closed** drift audit, wired into `infra-ci.yml` (like `deploy_guard_audit.py`). | Bidirectional: every inventory gate's `workflow:job` exists; every CI job is registered by exactly one gate; stage/category in the shared vocab; **no gate in both inventories**. The same audit ships to the app repo (both sides currently lack it). |
| **D4** | `MANIFEST.yaml` owner entry + a short `ops.pipeline.md` section linking the inventory and stating the app/infra line. | SSOT-first: register the new owner, point the narrative at the coordinate file. |
| **D5** | *(optional)* `tools/ci_chain_view.py` — join the two inventories on the stage axis → one `stage × task_category` delivery-chain matrix for high-level review. | This is the "review the whole pipeline in one table" payoff; read-only, no new source of truth. |

### 3. Out of scope (explicitly, to keep the line clean)

- **No copying app gates into infra's inventory** (or vice versa) — that would re-create
  the overlap this EPIC removes.
- **No second definition of the stage vocabulary** in infra's inventory — reference
  `delivery-stages.yaml` only.
- **No rewrite of GHA workflows.** The inventory is SSOT + audit-checked against the
  existing workflows; matrix-generation of jobs from the inventory is a *later, optional*
  step, not part of this EPIC.
- **Scheduled ops/drift workflows** map to `ops.*` stages in the inventory for
  completeness, but are **not** force-fit into the PR merge matrix — coordinate-izing
  must not imply they gate merges.

## Verification

- Deleting/renaming a CI job without updating the inventory → `ci_gate_audit` red.
- Adding a CI job not registered by any gate → audit red ("unregistered job").
- A gate whose `stage`/`task_category` is outside the shared vocab → audit red.
- The same gate id present in both repos' inventories → audit red (overlap).
- Both inventories `$ref`/reference one `delivery-stages.yaml`; no local `stages:` block
  remains in either.

## PR Links

_(to be filled as slices land)_

## Change Log

| Date | Change |
|------|--------|
| 2026-06-29 | Initialized: proposal for infra CI gate inventory + app/infra de-overlap. |

## References

- app SSOT: `docs/ssot/ci-gate-inventory.yaml`, `ci-cd.md`, `test-execution-matrix.yaml`, `delivery-gates.yaml`
- infra SSOT: `docs/ssot/ops.pipeline.md` (§4 `deploy_v2` coordinate, §5.4 Env×Stage result contract)
- Pattern to mirror: `tools/deploy_guard_audit.py` (fail-closed SSOT↔reality audit)
- Boundary parent: `AGENTS.md` (App emits artifact / Infra consumes)
