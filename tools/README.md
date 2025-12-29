# Infra2 CLI Tools

Standalone tools runnable via `invoke`.

## env_tool.py

Remote-first env/secret operations (1Password/Dokploy/Vault). No local `.env` sync.

```bash
# Read env vars
invoke env.get KEY --project=platform --env=production --service=postgres

# Write env vars
invoke env.set KEY=VALUE --project=platform --env=production

# Read secrets
invoke env.secret-get KEY --project=platform --env=production --service=postgres

# Write secrets
invoke env.secret-set KEY=VALUE --project=platform --env=production

# Preview merged values (project + environment + service)
invoke env.preview --project=platform --env=production --service=postgres
```
