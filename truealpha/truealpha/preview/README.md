# TrueAlpha Application — PREVIEW (multi-alias, ephemeral) compose

A throwaway, manually-deployed copy of the TrueAlpha app (web + llm). Unlike
staging/prod (one fixed Dokploy compose each), preview is a **family of aliases** —
`branch-<name>`, `pr-<N>`, `commit-<sha7>`, `tag-<slug>` — and each alias is its OWN
Dokploy compose stack with its OWN ephemeral database. Any number coexist; they outlive
a CI run until torn down. Mirrors `finance_report/finance_report/preview/` (#522
generalized `libs/deploy/preview.py` off a per-service registry — see
`libs/deploy_env_config.py::preview_service_config`).

## Files

| File | Purpose |
|------|---------|
| `compose.yaml` | App (vault-agent + llm + web) **plus** a bundled throwaway `db` (postgres) on a named volume. web/llm override `DATABASE_URL` to that local DB after sourcing Vault secrets. |
| `secrets.ctmpl` | Vault template. Reads app secrets (SEC_USER_AGENT, ANTHROPIC_API_KEY, S3) from a **fixed source env** (`PREVIEW_SECRET_ENV`, default `staging`). No postgres block — preview uses its own ephemeral DB. |
| `vault-agent.hcl` | AppRole auto-auth vault-agent config (identical pattern to `10.app`). |
| `vault-policy.hcl` | Policy granting read on the **source** env's `app` path only. |

## Lifecycle (manual)

```bash
python -m tools.deploy_v2 --service truealpha/app --type preview/pr --version-ref 5 --iac-ref main --domain zitian.party
python -m tools.deploy_v2 --service truealpha/app --type preview/commit --version-ref 1ab32d5 --iac-ref main --domain zitian.party
python -m tools.deploy_v2 --service truealpha/app --type preview/branch --version-ref main --iac-ref main --domain zitian.party

# Tear down an alias AND destroy its ephemeral DB volume
python -m tools.deploy_v2 --service truealpha/app --type preview/pr --version-ref 5 --iac-ref main --domain zitian.party --down
```

## Schema migration on boot

The `llm` service's image bakes in `db/migrations/*.sql` + `db/roles.sql` +
`db/apply_migrations.sh` (truealpha#428) and this compose's `llm` entrypoint runs it
against the ephemeral DB before serving — mirroring `Makefile`'s `db-migrate` target's
exact `psql -v ON_ERROR_STOP=1 -f` semantics. Every alias therefore boots migrated from
an empty database, the same schema-consistency guarantee finance_report's
`alembic upgrade head` gives its own preview.

## One-time LIVE setup (not unit-testable)

- A Dokploy `preview` environment under the `truealpha` project.
- A GitHub provider configured in Dokploy (the compose template is pulled from infra2).
- The preview AppRole's `VAULT_ADDR` / `VAULT_ROLE_ID` / `VAULT_SECRET_ID` supplied once
  on the compose env (preserved across redeploys; never set by the lifecycle), with the
  policy above bound to the source secret env (`invoke vault.setup-approle`).
- Wildcard DNS `*.zitian.party` + wildcard cert (already in place) route any
  `truealpha-<alias>` host automatically once the compose's Traefik labels exist.
