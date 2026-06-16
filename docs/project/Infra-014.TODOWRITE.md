# Infra-014: TODOWRITE (finance_report Observability)

**Status**: Active
**Owner**: Infra

## Purpose
Track top open items discovered during the finance_report observability wiring.

## Top Issues (Top 30)
- [ ] `platform/11.signoz/deploy.py`: `otel_ingest_token` is auto-generated on first deploy; the same token value must be exposed to the FE build/runtime so the browser sends `Authorization: Bearer <token>` — wire the FE side to read it (currently the public bearer is not yet surfaced to `NEXT_PUBLIC_*`). See RUNBOOK.
- [ ] `finance_report/finance_report/10.app/deploy.py`: `preview` OpenPanel client-id is a placeholder UUID; mint a real "preview" project and replace.
- [ ] `secret/platform/<env>/openpanel/api_key`: provision OpenPanel query API tokens per env for `common/observability/openpanel_query.py`.
- [ ] Consider a `libs/tests/` unit test asserting the otel-collector Traefik router carries both the bearer `Header()` match and the rate-limit middleware (mirrors `test_domain_routing_policy.py`).
- [ ] Validate CORS allowlist stays in sync with FE preview alias domains as new alias shapes are added.

> **Note**: When archiving this project, merge this TODOWRITE into the archived project file.
