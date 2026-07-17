# Infra-016: CI Gate Inventory — coordinate-ize infra CI, and de-overlap app vs infra responsibilities

**Status**: In Progress

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
| Env×Stage **result** contract | (app CI emits) | ✅ `infra2_sdk.delivery` (§5.4) |
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

> **Contracts (schema + shared vocabulary) have exactly one owner — `infra2-sdk`. Gate
> *instances* are owned by whoever runs them, and no gate is listed in both inventories.**

| Asset | Single owner | The other side |
|---|---|---|
| Delivery **stage vocabulary** (`local.* / github_ci.* / preview.* / staging.* / prod.* / ops.* / manual.*`) | **infra2-sdk** | infra keeps an equality-guarded SSOT mirror; app imports the SDK |
| Gate inventory **schema** (the gate object's fields) | **infra2-sdk** | infra and app *conform* |
| **Env×Stage result** contract (`infra2_sdk.delivery`) | **infra2-sdk** | infra and app *emit* into it |
| **`deploy_v2` coordinate** (`service, type, version_ref, iac_ref`) | **infra** (already, §4) | app *calls* |
| **app gate instances** (backend / frontend / AC / coverage gates) | **app** (`ci-gate-inventory.yaml`) | infra never lists them |
| **infra gate instances** (compose / vault / deployer / reconcile / preview-leak) | **infra** (new `ci-gate-inventory.yaml`) | app never lists them |
| **`task_category` vocabulary** | each owns its own subset (app: `backend_*/frontend_*`; infra: `compose_*/vault_*`) | shared *namespace*, disjoint values |

Three non-overlap invariants (enforced by audit, §3):

1. **Every gate appears in exactly one inventory.** An app gate never appears in infra's
   inventory, and vice versa. No "transitional duplicate" survives across the line.
2. **The `stage` axis is the only shared vocabulary, and it is single-owner (`infra2-sdk`).**
   Both inventories validate against their pinned SDK version; infra's
   `delivery-stages.yaml` is an equality-guarded SSOT mirror, not a second machine contract.
3. **Cross-repo views are produced by joining on the shared stage axis, never by
   copying.** The full delivery-chain matrix is a *read-time join* of the two
   inventories, not a third hand-maintained list.

Why infra publishes the contracts through `infra2-sdk`: `infra2` is the delivery platform/artifact — it defines the
*shape* of the pipeline (stages, deploy coordinate, result schema). The app is a consumer
that fills in its own gates. This mirrors the established **App emits artifact / Infra
consumes** boundary in `AGENTS.md`; here the same arrow means *infra defines the pipeline
contract, app declares its gates inside it*.

### 2. Infra2 deliverables

| ID | Deliverable | Notes |
|----|-------------|-------|
| **D1** | `infra2_sdk.ci` stage vocabulary plus `docs/ssot/delivery-stages.yaml` mirror. | App pins the SDK; infra equality-checks the local SSOT mirror against that release. |
| **D2** | `docs/ssot/ci-gate-inventory.yaml` (infra) — coordinate-ize the ~23 CI/validation jobs (`infra-ci` 7, `ops-checks` 6, reconcile dry-run gate, apply-observability, drift reports). Same schema as app. | infra `task_category` values: `compose_validate, vault_policy, vault_agent, deployer_contract, secret_preflight, op_healthcheck, reconcile_plan, preview_leak, drift_report, …` |
| **D3** | `tools/ci_gate_audit.py` — **fail-closed** drift audit, wired into `infra-ci.yml` (like `deploy_guard_audit.py`). | Bidirectional: every inventory gate's `workflow:job` exists; every CI job is registered by exactly one gate; stage/category in the shared vocab; **no gate in both inventories**. The same audit ships to the app repo (both sides currently lack it). |
| **D4** | `MANIFEST.yaml` owner entry + a short `ops.pipeline.md` section linking the inventory and stating the app/infra line. | SSOT-first: register the new owner, point the narrative at the coordinate file. |
| **D5** | *(optional)* `tools/ci_chain_view.py` — join the two inventories on the stage axis → one `stage × task_category` delivery-chain matrix for high-level review. | This is the "review the whole pipeline in one table" payoff; read-only, no new source of truth. |

### 3. Out of scope (explicitly, to keep the line clean)

- **No copying app gates into infra's inventory** (or vice versa) — that would re-create
  the overlap this EPIC removes.
- **No second machine definition of the stage vocabulary** in either inventory — import
  the pinned SDK and keep infra's documented mirror equality-guarded.
- **No rewrite of GHA workflows.** The inventory is SSOT + audit-checked against the
  existing workflows; matrix-generation of jobs from the inventory is a *later, optional*
  step, not part of this EPIC.
- **Scheduled ops/drift workflows** map to `ops.*` stages in the inventory for
  completeness, but are **not** force-fit into the PR merge matrix — coordinate-izing
  must not imply they gate merges.

## Implementation plan — incremental, reversible, machine-verified

### Engineering principles (govern every step)

1. **Contract before instances** — the schema and stage vocabulary land before any gate is written against them.
2. **Audit ratchet: report-only → fail-closed.** A drift audit ships first as non-blocking (prints diffs, exits 0), and only flips to blocking once the inventory is clean. No single step turns CI red on existing work.
3. **One step = one PR, system stays green, independently revertible.** Every PR is a no-op or additive to runtime behavior until the ratchet closes.
4. **Acceptance is machine-decidable** — an audit exit code, a test, or a CI gate. Never "looks complete".
5. **Cross-repo changes (touching app) come last,** paired and behind a compat transition proven to be a no-op.
6. **Non-overlap is structural, not runtime.** Each gate id is namespaced by its owning repo (`infra_ci.*` vs app's `ci.*/preview.*/…`). Each repo's audit asserts *all its gates carry its own prefix*, so two repos **cannot** register the same id — "no overlap" becomes a single-repo, machine-decidable check needing no cross-repo lookup. Cross-repo views consume versioned contract artifacts and explicit inventory inputs; App repositories never vendor infra2 source.

### Phase overview

| Phase | PR(s) | Lands | Acceptance gate (machine-decidable) | Rollback |
|-------|-------|-------|-------------------------------------|----------|
| **0 Contracts** | infra ×1 | SDK CI schema + `delivery-stages.yaml` + MANIFEST | schema validates a good/bad fixture; stages unique+ordered+named; MANIFEST guard green | revert PR (additive) |
| **1 Inventory (shadow)** | infra ×1 | infra `ci-gate-inventory.yaml` (23 jobs) + `ci_gate_audit --report` non-blocking | every entry validates; `dangling_gates==[]` (test-enforced); `unregistered_jobs` listed as backlog | drop audit step |
| **2 Fail-closed (ratchet closes)** | infra ×1 | backfill gaps; audit `exit 1`; blocking infra-ci step | `unregistered_jobs==[]` **and** `dangling_gates==[]`; negative tests prove red; blocking gate present | flip audit back to report-only (1 line) |
| **3 App alignment** | app ×2 | 3a: shared schema + app-side audit; 3b: app references `delivery-stages.yaml` | app audit fail-closed green; **(gate_id→stage) byte-identical before/after 3b** (no-op migration test); app has no local `stages:` | 3a/3b independently revertible |
| **4 Non-overlap + view** | app ×1 | `ci_chain_view.py` (join on stage) + cross-repo prefix-disjoint guard | view covers 100% of both inventories (count match); duplicate id → guard red; matrix rendered into docs | revert PR |

### Per-phase Definition of Done

**Phase 0 — Contracts (infra only, zero behavior change).**
- `infra2_sdk.ci` loads; `test_ci_gate_schema` accepts a valid gate fixture and rejects one missing a required field / with an unknown stage.
- `docs/ssot/delivery-stages.yaml`: stage ids unique, carry an explicit `order`, match `^[a-z]+\.[a-z_]+$`; `test_delivery_stages` enforces all three.
- `MANIFEST.yaml` gains owner entries for both files; the existing MANIFEST-consistency guard stays green. *Accept = the three tests + MANIFEST guard pass.*

**Phase 1 — Infra inventory in shadow.**
- Reverse-engineer the 23 infra CI jobs (`infra-ci` 7, `ops-checks` 6, reconcile dry-run gate, apply-observability, drift reports) into `docs/ssot/ci-gate-inventory.yaml`, each conforming to the schema.
- `tools/ci_gate_audit.py --report` emits `{dangling_gates, unregistered_jobs, gaps}` JSON, exit 0, wired **non-blocking** in `infra-ci.yml`.
- *Accept = (a) every entry passes schema; (b) `dangling_gates == []` is hard-enforced by a test even in shadow (a listed gate must point at a real `workflow:job`); (c) `unregistered_jobs` may be non-empty and is printed as the gap backlog.*

**Phase 2 — Infra audit fail-closed (the done line for infra).**
- Backfill until `unregistered_jobs == []`.
- Flip audit to `exit 1` on any drift; make the `infra-ci.yml` step **blocking**.
- `test_ci_gate_audit` carries negatives: remove a job → exit 1; add an unlisted job → exit 1; gate with stage ∉ vocab → exit 1; gate id without the `infra_ci.` prefix → exit 1.
- *Accept = `unregistered_jobs==[] && dangling_gates==[]`, all four negative tests red-on-drift, blocking step present. Proven by a broken fixture in the test suite, not by inspection.*

**Phase 3 — App alignment (cross-repo, highest risk, compat transition).**
- *3a (additive):* app pins the released `infra2-sdk` CI schema and gains its own `ci_gate_audit`; app keeps its local stages. Accept = app audit fail-closed green.
- *3b (source-swap):* replace app's embedded `stages:` block with a reference to `delivery-stages.yaml`; assert app's used stages ⊆ the shared vocab.
- *Accept = a **no-op migration test**: the set of `(gate_id → stage)` is byte-identical before and after 3b — only the *source* of the vocabulary changed, never a value. This makes the riskiest step provably behavior-preserving.*

**Phase 4 — Cross-repo non-overlap + chain view.**
- In the **app repo**, `tools/ci_chain_view.py` joins explicit App and infra inventory inputs on the SDK-owned stage axis into one `stage × task_category` matrix; a guard asserts the two gate-id sets are prefix-disjoint.
- *Accept = the rendered matrix covers 100% of both inventories' gates (gate count == sum); a constructed duplicate id makes the guard red; the matrix is embedded into `ci-cd.md` / `ops.pipeline.md` as the single high-level review surface.*

### Critical path & risk

`0 → 1 → 2` is entirely inside infra and ships the whole coordinate-ized review surface
**without touching app**; Phase 2 is the infra done line. `3` is the only cross-repo,
prod-CI-touching work — de-risked by `3a` being additive and `3b` being a proven no-op.
`4` is pure read-time aggregation. Any phase reverts independently; the ratchet (principle
2) guarantees the merge-blocking behavior appears only when the inventory is already clean.

## Verification

- Deleting/renaming a CI job without updating the inventory → `ci_gate_audit` red.
- Adding a CI job not registered by any gate → audit red ("unregistered job").
- A gate whose `stage`/`task_category` is outside the shared vocab → audit red.
- The same gate id present in both repos' inventories → audit red (overlap).
- Both inventories `$ref`/reference one `delivery-stages.yaml`; no local `stages:` block
  remains in either.

## Tracking

- **Root epic:** infra2#459 — CI verification → coordinate contracts.
- **Sub-issues:** infra2#460 (contracts), infra2#461 (infra inventory+audit), finance_report#1491 (app align + retire mirror-asserts), finance_report#1492 (cross-repo non-overlap+view).
- **Consolidates:** finance_report#1435 (CI over-mirrored root cause), finance_report#876 (App/Infra boundary), infra2#280 (delivery hardening).

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
