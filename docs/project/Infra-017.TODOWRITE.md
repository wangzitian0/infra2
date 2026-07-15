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
