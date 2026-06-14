# Finance Report — PREVIEW (multi-alias, ephemeral) compose

> Module slice: the **preview** deploy target. EPIC anchor: `docs/project/Infra-009.*`.
> Canonical contract: `docs/ssot/core.environments.md` §4.6 (manual deploy targets).

A throwaway, manually-deployed copy of the Finance Report app. Unlike staging/prod
(one fixed Dokploy compose each), preview is a **family of aliases** — `main`,
`pr-<N>`, `commit-<sha7>` — and each alias is its OWN Dokploy compose stack with its
OWN ephemeral database. Any number coexist; they outlive a CI run until torn down.

## Files

| File | Purpose |
|------|---------|
| `compose.yaml` | App (vault-agent + backend + frontend) **plus** a bundled throwaway `db` (postgres) on a named volume. Backend overrides `DATABASE_URL` to that local DB after sourcing Vault secrets. |
| `secrets.ctmpl` | Vault template. Reads app secrets (AI keys, S3, OTEL) from a **fixed source env** (`PREVIEW_SECRET_ENV`, default `staging`). No postgres/redis blocks — preview uses its own ephemeral DB. |
| `vault-agent.hcl` | AppRole auto-auth vault-agent config (identical pattern to `10.app`). |
| `vault-policy.hcl` | Policy granting read on the **source** env's `app` path only. |

## Lifecycle (manual)

Driven by `tools/preview_lifecycle.py` (over the existing `libs/dokploy.py` client):

```bash
# Stand up / update + deploy an alias, then health-check report-<alias>/api/health
python -m tools.preview_lifecycle up --kind pr --value 5 --code main --domain zitian.party
python -m tools.preview_lifecycle up --kind commit --value 1ab32d5 --code 1ab32d5 --domain zitian.party
python -m tools.preview_lifecycle up --kind main --code main --domain zitian.party

# Tear down an alias AND destroy its ephemeral DB volume
python -m tools.preview_lifecycle down --kind pr --value 5 --domain zitian.party
```

The alias → {env_suffix, domain, compose slug, telemetry label} mapping is the pure,
unit-tested `tools/deploy_env_config.py::preview_alias(kind, value)`.

## Ephemeral DB

The `db` service stores data on the **named** `preview_db` volume (no host bind mount),
so `down` (`delete_compose(delete_volumes=True)`) removes it entirely. Migrations run on
backend startup (`alembic upgrade head`) against the fresh DB. Preview NEVER reads or
writes the shared staging/prod database.

## One-time LIVE setup (not unit-testable)

- A Dokploy `preview` environment under the `finance_report` project.
- A GitHub provider configured in Dokploy (the compose template is pulled from infra2).
- The preview AppRole's `VAULT_ADDR` / `VAULT_ROLE_ID` / `VAULT_SECRET_ID` supplied once
  on the compose env (preserved across redeploys; never set by the lifecycle), with the
  policy above bound to the source secret env.
- Wildcard DNS `*.zitian.party` + wildcard cert (already in place) route any
  `report-<alias>` host automatically once the compose's Traefik labels exist.
