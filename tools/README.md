# Infra2 CLI Tools

Standalone `invoke` namespaces loaded by `tools/loader.py`.

## Runner

- Use `invoke` inside an activated venv, or prefix with `uv run` when using uv.
- List all tasks: `invoke --list` (未激活虚拟环境时用 `uv run invoke --list`).

## Namespaces

| Namespace | Entry | Purpose |
|-----------|-------|---------|
| `env` | `tools/env_tool.py` | Remote env/secret SSOT operations |
| `dokploy` | `tools/dokploy_env.py` | Dokploy project/environment helpers |
| `dokploy_route_canary.py` | `tools/dokploy_route_canary.py` | Fast-fail Dokploy worker, Docker, Traefik, and public route probe |
| `out_of_band_watchdog.py` | `tools/out_of_band_watchdog.py` | GitHub-hosted direct Feishu watchdog for host, bridge, Worker, and Dokploy route-canary liveness |
| `local` | `tools/local_init.py` | Local CLI checks and bootstrap helpers |
| `vault-audit` | `tools/vault_audit.py` | Read-only Vault app-token self-refresh audit |

## Common Conventions

- Pattern: `invoke <namespace>.<task>`
- `env` defaults: `--env=production`, `--service` optional (required for `list-all`)
- Write operations use `KEY=VALUE` (quote values with spaces)
- Output uses `libs.console` helpers; avoid raw `print` in new tasks.
- Omit `--service` for environment-level (`{project}/{env}`) values.

## env (remote secrets)

Remote-first secrets operations (1Password/Vault). No local `.env` sync.

```bash
# Read secret
invoke env.get KEY --project=platform --service=postgres

# Write secret
invoke env.set KEY=VALUE --project=platform --service=postgres

# List secrets (masked)
invoke env.list-all --project=platform --service=postgres

# Show init/env_vars from 1Password
invoke env.init-status
```

## dokploy (project/environment)

```bash
# List environments for a project
invoke dokploy.env-list --project=platform

# Ensure staging environment exists
invoke dokploy.env-ensure --project=platform --env=staging --description="staging env"
```

## dokploy_route_canary.py

Dynamic route materialization proof for the Dokploy platform. It deploys a
minimal same-host web/API compose and returns JSON that classifies failures as
control plane, compose source-type drift, deployment record/worker, Docker
runtime, or public Traefik route failures.

```bash
python tools/dokploy_route_canary.py \
  --host route-canary.zitian.party \
  --environment-id="$DOKPLOY_ENVIRONMENT_ID" \
  --project platform \
  --env staging \
  --dokploy-host cloud.zitian.party \
  --repair-stale-compose
```

`--repair-stale-compose` is restricted to `route-canary*` hosts and
`dokploy-route-canary*` compose names. Repaired composes are normalized back to
`sourceType=raw` before redeploying. GitHub canary runs default to the stable
canary host/compose and rely on workflow concurrency to avoid overlap.

## out_of_band_watchdog.py

Direct Feishu watchdog intended to run outside the infra2 host from GitHub
Actions. It verifies public host reachability, Cloudflare Worker self-health,
SSH diagnostics, and the Dokploy route canary.

```bash
INFRA2_WATCHDOG_DRY_RUN=1 uv run python tools/out_of_band_watchdog.py
```

## local (local readiness + bootstrap)

- 输出统一使用 `libs.console`（状态行 + 命令块），不直接 `print`。

```bash
# Check CLI dependencies
invoke local.check

# Guide local setup (prints install instructions)
invoke local.init

# Show installed CLI versions
invoke local.version

# Validate init/env_vars in 1Password (no local .env)
invoke local.bootstrap

# Detect current bootstrap phase
invoke local.phase
```

## vault-audit (Vault runtime proof)

Read-only audit for the Vault app-token self-refresh chain. It checks Dokploy
env, Vault token lookup, rendered `/vault/secrets/.env` freshness, vault-agent
logs, and container state for every service in
`docs/ssot/vault-self-refresh-inventory.yaml`.

```bash
# Live production audit
invoke vault-audit.self-refresh

# Audit one inventory row
invoke vault-audit.self-refresh --service=finance_report/app

# Machine-readable output
invoke vault-audit.self-refresh --json-output

# Offline classifier test from a captured observation fixture
invoke vault-audit.self-refresh --observations=/path/to/observations.json
```

## References

- [文档索引](../docs/README.md)
- [SSOT Index](../docs/ssot/README.md)
- [Project Portfolio](../docs/project/README.md)
- [AI 行为准则](../AGENTS.md)
