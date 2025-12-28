# Infra2 CLI Tools

Standalone tools runnable via `invoke`.

## env_sync.py

Sync environment variables between local `.env` files and Vault.

```bash
# Push local env to Vault
invoke env.push --project=platform --env=production --level=service

# Pull from Vault to local
invoke env.pull --project=platform --env=production --level=service

# Show status
invoke env.status --project=platform --env=production
```

### Levels

| Level | Local File | Vault Path |
|-------|------------|------------|
| project | `.env` | `secret/{project}/` |
| environment | `.env.{env}` | `secret/{project}/{env}/` |
| service | `{project}/.env.{env}` | `secret/{project}/{env}/service/` |
