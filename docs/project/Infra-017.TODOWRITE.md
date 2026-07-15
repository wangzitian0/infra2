# Infra-017: TODOWRITE (TrueAlpha Dagster Capture)

**Status**: Active
**Owner**: Infra

## Purpose

Track deployment and evidence gaps until the Staging capture gate is accepted.

## Top Issues (Top 30)

- [ ] Publish and record the reviewed `truealpha-data-engine` OCI digest.
- [ ] Populate environment-scoped data-engine Vault fields without copying Production credentials into Staging.
- [ ] Verify the S3 API endpoint is authenticated and no administrative interface is exposed by this service.
- [ ] Run the two scheduled TOPT cycles, identical retry, changed vintage, and failure injection.
- [ ] Add Production definitions only after the approved catalog/universe/release exists.
- [x] Publish the task-name normalization hotfix as `v1.1.30` and verify the runner executes `ta-data-engine.sync`.
- [ ] Rerun the full Staging reconcile after the environment-scoped data-engine Vault fields exist; do not promote the data engine before that gate passes.
- [ ] Make IaC Runner deploy status/cache identity service-set aware and reload checkout-derived task discovery after a ref change.

## Deployment Evidence

- 2026-07-16: `v1.1.29` Staging reconcile run `29433249921` deployed
  `platform/alerting` and `truealpha/app`, then failed closed before Production because
  discovery emitted `ta-data_engine.sync` while Invoke exposes `ta-data-engine.sync`.
- 2026-07-16: Addressed the High review finding in the task-discovery regression test
  while retaining compatibility with Invoke 2.2.1, where `Collection.task_names` is a property.
- 2026-07-16: Published `v1.1.30` at reviewed `main` SHA `fe40be703751`; automatic
  Staging reconcile run `29440207049` completed the release delta for `platform/alerting`.
- 2026-07-16: Refreshed the IaC Runner through the L1 external bootstrap path in run
  `29440496491`. Compensation run `29440618091` then proved the normalized
  `ta-data-engine.sync` task is executable and failed closed on the next declared gate:
  missing `secret/truealpha/staging/data_engine`.
- 2026-07-16: Production promotion run `29440732131` applied only the successful
  `v1.1.29..v1.1.30` release delta (`platform/alerting`); TrueAlpha data-engine
  Production remained untouched.
