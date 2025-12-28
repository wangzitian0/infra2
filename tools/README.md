# Infra2 CLI Tools

Standalone tools runnable via `invoke`.

## env_sync.py

Sync environment variables between local `.env` files and Vault.

```bash
# Push local .env to Vault
invoke env.push --level=service --env=prod --service=postgres

# Pull from Vault to local
invoke env.pull --level=service --env=prod --service=postgres

# Show status
invoke env.status --service=postgres
```

### Levels

| Level | Local File | Vault Path |
|-------|------------|------------|
| project | `.env` | `secret/platform/` |
| environment | `.env.{env}` | `secret/platform/{env}/` |
| service | `platform/{svc}/.env.{env}.local` | `secret/platform/{env}/{svc}/` |
