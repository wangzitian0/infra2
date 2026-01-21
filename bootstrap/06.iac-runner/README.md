# IaC Runner

GitOps-style webhook service that syncs infrastructure when main branch changes.

## How It Works

```
┌─────────────┐     webhook      ┌──────────────┐     invoke sync   ┌─────────────┐
│   GitHub    │ ──────────────▶ │  IaC Runner  │ ─────────────────▶│  Services   │
│  (push)     │                  │  (container) │                   │  (Dokploy)  │
└─────────────┘                  └──────────────┘                   └─────────────┘
```

1. Developer pushes to `main` branch
2. GitHub sends webhook to `https://iac.{domain}/webhook`
3. IaC Runner parses changed files to identify affected services
4. Runs `invoke {service}.sync` for each changed service
5. Sync task compares config hash and only redeploys if changed

## Idempotency

The `sync` task uses config hashing:
- Computes SHA256 of `compose.yaml + env vars`
- Stores hash as `IAC_CONFIG_HASH` in Dokploy env
- Skips deploy if hash matches (no changes)

## Architecture

Uses **vault-agent sidecar** pattern for secrets injection:

```
┌─────────────────────────────────────────────────────────────────┐
│                       IaC Runner Pod                            │
│  ┌──────────────┐    tmpfs    ┌─────────────────────────────┐   │
│  │ vault-agent  │───────────▶│     IaC Runner              │   │
│  │ (sidecar)    │ /secrets   │  - webhook_server.py        │   │
│  └──────────────┘            │  - sync_runner.py           │   │
│         │                    └─────────────────────────────┘   │
│         ▼                                                       │
│  Vault (fetch WEBHOOK_SECRET, GIT_REPO_URL)                     │
└─────────────────────────────────────────────────────────────────┘
```

## Setup

### 1. Store Secrets in Vault

```bash
# Store webhook secret and git repo URL
invoke env.set WEBHOOK_SECRET=$(openssl rand -hex 32) --project=bootstrap --service=iac_runner
invoke env.set GIT_REPO_URL=https://github.com/wangzitian0/infra2.git --project=bootstrap --service=iac_runner
```

Vault path: `secret/data/bootstrap/production/iac_runner`

### 2. Generate VAULT_APP_TOKEN

```bash
export VAULT_ROOT_TOKEN=$(op read 'op://Infra2/dexluuvzg5paff3cltmtnlnosm/Token')
invoke vault.setup-tokens
```

This creates a read-only token for IaC Runner and auto-configures it in Dokploy.

### 3. Configure GitHub Webhook

1. Go to repo Settings → Webhooks → Add webhook
2. Payload URL: `https://iac.{your-domain}/webhook`
3. Content type: `application/json`
4. Secret: (the WEBHOOK_SECRET from Vault)
5. Events: Just the push event

### 4. Deploy

```bash
invoke iac-runner.setup
```

## Version-Based Deployment

IaC Runner supports **GitOps version-based deployments** via GitHub Actions workflows.

### Versioning Strategy

**Semantic Versioning**: `v{major}.{minor}.{patch}`

- **Patch**: Staging iterations (auto-incremented on every `main` push)
- **Minor**: Production releases (manual promotion from staging)
- **Major**: Architecture changes (rare, manual)

### Deployment Flows

**Staging (Automatic)**:
```
Push to main → platform-staging.yml → v1.2.{patch+1} → Deploy to Staging
```

**Production (Manual)**:
```
Promote staging tag → platform-production.yml → v1.{minor+1}.0 → Deploy to Production
```

**Hotfix (Manual)**:
```
Create from prod tag → v1.3.1 → Deploy to Production (no main merge required)
```

### Example Workflow

1. **Developer pushes to main**:
   ```bash
   git add platform/01.postgres/compose.yaml
   git commit -m "feat: update postgres config"
   git push origin main
   ```

2. **GitHub Actions auto-tags and deploys**:
   - Reads latest tag (e.g., `v1.2.3`)
   - Increments patch: `v1.2.4`
   - Creates git tag
   - Calls `/deploy` endpoint with `{"env":"staging","tag":"v1.2.4"}`

3. **IaC Runner deploys to staging**:
   - Checks out tag `v1.2.4`
   - Runs `invoke {service}.sync` for all platform services
   - Each service compares config hash and deploys only if changed

4. **Manual production promotion**:
   ```bash
   gh workflow run platform-production.yml \
     -f confirm="deploy" \
     -f staging_tag="v1.2.4"
   ```
   - Creates production tag `v1.3.0` (minor +1)
   - Calls `/deploy` endpoint with `{"env":"production","tag":"v1.3.0"}`
   - Creates GitHub Release

## Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/webhook` | POST | GitHub webhook receiver (change-based sync) |
| `/sync` | POST | Manual sync trigger (legacy) |
| `/deploy` | POST | Version-based deployment (GitOps) |

### Version-Based Deployment

```bash
# Deploy specific version to staging
PAYLOAD='{"env":"staging","tag":"v1.2.4","triggered_by":"github-actions"}'
SIGNATURE=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac 'YOUR_SECRET' | awk '{print $2}')
curl -X POST https://iac.{domain}/deploy \
  -H "Content-Type: application/json" \
  -H "X-Hub-Signature-256: sha256=$SIGNATURE" \
  -d "$PAYLOAD"

# Deploy specific version to production
PAYLOAD='{"env":"production","tag":"v1.3.0","triggered_by":"manual-promotion"}'
SIGNATURE=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac 'YOUR_SECRET' | awk '{print $2}')
curl -X POST https://iac.{domain}/deploy \
  -H "Content-Type: application/json" \
  -H "X-Hub-Signature-256: sha256=$SIGNATURE" \
  -d "$PAYLOAD"
```

### Manual Sync (Legacy)

```bash
# Sync specific services (requires signature)
PAYLOAD='{"services":["platform/postgres"]}'
SIGNATURE=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac 'YOUR_SECRET' | cut -d' ' -f2)
curl -X POST https://iac.{domain}/sync \
  -H "Content-Type: application/json" \
  -H "X-Hub-Signature-256: sha256=$SIGNATURE" \
  -d "$PAYLOAD"

# Sync all (requires signature)
PAYLOAD='{"all": true}'
SIGNATURE=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac 'YOUR_SECRET' | cut -d' ' -f2)
curl -X POST https://iac.{domain}/sync \
  -H "Content-Type: application/json" \
  -H "X-Hub-Signature-256: sha256=$SIGNATURE" \
  -d "$PAYLOAD"
```

## Scope

IaC Runner **ONLY manages `platform` project services**.

| Project | Management Method |
|---------|------------------|
| **Bootstrap** (1Password, Vault, IaC Runner) | Manual deployment (avoid circular deps) |
| **Platform** (postgres, redis, authentik, etc.) | Auto-sync via IaC Runner |
| **Apps** (finance_report, wealthfolio) | Own GitHub CI/CD pipelines |

## Service Mapping

| Changed Path | Invoke Task | Notes |
|--------------|-------------|-------|
| `platform/01.postgres/*` | `postgres.sync` | Auto-sync |
| `platform/10.authentik/*` | `authentik.sync` | Auto-sync |
| `libs/*` | All platform services | Full platform sync |
| `bootstrap/*` | Skipped | Manual only |
| `finance_report/*` | Skipped | Use finance_report CI |
| `finance/*` | Skipped | Use app-specific CI |

## Troubleshooting

### Vault Token Not Persisting

**Symptom**: After container restart, vault-agent fails to authenticate

**Cause**: Dokploy GitHub provider doesn't persist environment variables set via API

**Workaround**: Manually inject token when restarting:
```bash
ssh root@$VPS_HOST
export VAULT_APP_TOKEN='<your-vault-token-here>'
cd /etc/dokploy/compose/bootstrap-iac_runner-bkewyn/code/bootstrap/06.iac-runner
docker compose down && docker compose up -d
```

**Long-term solution options**:
1. Switch to Dokploy Docker Compose provider (not GitHub provider)
2. Use Vault AppRole authentication (more complex)
3. File bug with Dokploy team

### Git Safe Directory Error

**Symptom**: `fatal: detected dubious ownership in repository at '/workspace/infra2'`

**Fix**: Permanent fix in PR #79 - adds git config to Dockerfile

### Workspace State Issues

**Symptom**: `error: Your local changes to the following files would be overwritten by checkout`

**Fix**: Permanent fix in PR #79 - adds `git reset --hard HEAD` before checkout

## Security Considerations

- HMAC signature verification for all requests
- Read-only docker socket mount
- No write access to host filesystem (except workspace)
- Bootstrap services excluded from auto-sync
