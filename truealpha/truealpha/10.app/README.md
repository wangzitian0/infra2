# TrueAlpha Application (web + llm)

Two containers from the truealpha app repo's GHCR images
(`ghcr.io/wangzitian0/truealpha-app-web`, `ghcr.io/wangzitian0/truealpha-llm-service`):

- **web** — Next.js (standalone) at `truealpha${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}`.
  Reads the Postgres `mart` schema directly (the app's init.md rule 5) via the
  Vault-rendered `DATABASE_URL`; there is no web→llm API hop.
- **llm** — FastAPI at the same host under `PathPrefix(/api)` (stripped). Scope is
  LLM-call orchestration only: MCP endpoint first, `/chat` later (Tier 3).
- **data-engine is NOT deployed here** — Phase -1 sampling runs locally; Dagster
  scheduling arrives with the app's Phase 3 and will get its own service dir.

## v1 deploy model (deliberate simplification)

`iac_pinned` like a platform service: `deploy_v2 --service truealpha/app --type
staging --iac-ref vX.Y.Z` → iac_runner → `invoke ta-app.sync` → Dokploy compose.
Images float on `${IMAGE_TAG:-latest}` (pushed by the app repo's CI on main).
The finance_report-style promote path (pinned tags, rollout verification,
per-PR previews) is deferred until there is real mart data to protect.

## Deploy

```bash
invoke env.set SEC_USER_AGENT='TrueAlpha research <email>' --project=truealpha --service=app
export VAULT_ROOT_TOKEN=$(op read 'op://Infra2/bootstrap/vault/Root Token/Root Token')
invoke vault.setup-approle --project=truealpha --service=app
python -m tools.deploy_v2 --service truealpha/app --type staging --iac-ref vX.Y.Z --domain zitian.party
invoke ta-app.shared.status
curl https://truealpha-staging.zitian.party/api/health
```
