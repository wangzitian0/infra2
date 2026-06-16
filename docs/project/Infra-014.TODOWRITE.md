# Infra-014: TODOWRITE (finance_report Observability)

**Status**: Active
**Owner**: Infra

## Purpose
Track top open items discovered during the finance_report observability wiring.

## Top Issues (Top 30)
- [x] ~~`platform/11.signoz/deploy.py`: wire the browser bearer to `NEXT_PUBLIC_*`.~~ Dropped (follow-up): a browser cannot hold a secret, so the bearer was removed entirely; ingest is gated by CORS + collector limits, no token plumbing.
- [ ] `finance_report/finance_report/10.app/deploy.py`: `preview` OpenPanel client-id is a placeholder UUID; mint a real "preview" project and replace.
- [ ] `secret/platform/<env>/openpanel/api_key`: provision OpenPanel query API tokens per env for `common/observability/openpanel_query.py`.
- [ ] Edge per-IP rate limit for the public OTLP ingest — add as a **Dokploy-managed** Traefik ratelimit middleware (NOT a hand-written compose label) to cap burst abuse of the unauthenticated endpoint.
- [ ] Validate CORS allowlist stays in sync with FE preview alias domains as new alias shapes are added.

> **Note**: When archiving this project, merge this TODOWRITE into the archived project file.
