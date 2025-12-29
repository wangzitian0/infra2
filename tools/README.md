# Infra2 CLI Tools

Standalone tools runnable via `invoke`.

## env_tool.py

Remote-first env/secret operations (1Password/Dokploy/Vault). No local `.env` sync.

```bash
# Read/set env vars
invoke env.get KEY --project=platform --env=production --service=postgres
invoke env.set KEY=VALUE --project=platform --env=production --service=postgres

# Read/set secrets
invoke env.secret-get KEY --project=platform --env=production --service=postgres
invoke env.secret-set KEY=VALUE --project=platform --env=production --service=postgres

# Preview merged values (project + environment + service)
invoke env.preview --project=platform --env=production --service=postgres

# Copy env/secrets between environments
invoke env.copy --from-project=platform --from-env=production --to-env=staging --service=postgres
```
