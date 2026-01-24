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

## Security Considerations

- HMAC signature verification for all requests
- Read-only docker socket mount
- No write access to host filesystem (except workspace)
- Bootstrap services excluded from auto-sync

---

## Troubleshooting

### Issue 1: `FileNotFoundError: 'op'`

**Symptoms**:
```
FileNotFoundError: [Errno 2] No such file or directory: 'op'
```

**Cause**: Container missing 1Password CLI

**Fixed in**: PR #101

**Solution**: Dockerfile now includes op CLI installation:
```dockerfile
# Install 1Password CLI (required by libs/common.py::OpSecrets)
RUN curl -sSfLo op.zip https://cache.agilebits.com/dist/1P/op2/pkg/v2.30.0/op_linux_amd64_v2.30.0.zip && \
    unzip -od /usr/local/bin/ op.zip && \
    rm op.zip && \
    chmod +x /usr/local/bin/op
```

**Verification**:
```bash
docker exec iac-runner which op
# Expected: /usr/local/bin/op
```

### Issue 2: `unzip: not found`

**Symptoms**:
```
/bin/sh: 1: unzip: not found
```

**Cause**: `python:3.11-slim` base image doesn't include `unzip`

**Fixed in**: PR #102

**Solution**: Dockerfile now installs unzip:
```dockerfile
RUN apt-get update && apt-get install -y \
    git \
    unzip \
    && rm -rf /var/lib/apt/lists/*
```

### Issue 3: VAULT_APP_TOKEN not found

**Symptoms**: Container logs show Vault connection errors

**Cause**: Missing or invalid VAULT_APP_TOKEN

**Solution**:
```bash
# Regenerate token
export VAULT_ROOT_TOKEN=$(op read 'op://Infra2/dexluuvzg5paff3cltmtnlnosm/Token')
invoke vault.setup-tokens

# Restart container
docker restart iac-runner
```

**Verification**:
```bash
docker exec iac-runner env | grep VAULT_APP_TOKEN
# Should show token value
```

### Issue 4: Webhook signature verification failed

**Symptoms**: GitHub webhook returns 403 Forbidden

**Diagnosis**:
```bash
# 1. Check secret in Vault
invoke env.get WEBHOOK_SECRET --project=bootstrap --service=iac_runner

# 2. Compare with GitHub webhook config
# Settings → Webhooks → Check Secret matches

# 3. Test signature manually
PAYLOAD='{"ref":"refs/heads/main"}'
SECRET="<WEBHOOK_SECRET>"
SIGNATURE=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')
echo "Expected: sha256=$SIGNATURE"
```

---

## Health Checks

```bash
# Container status
docker ps --filter name=iac-runner
# Expected: iac-runner (Up, healthy)
# Expected: iac-runner-vault-agent (Up, healthy)

# Health endpoint
curl https://iac.{domain}/health
# Expected: {"status":"healthy"}

# Vault Agent status
docker logs iac-runner-vault-agent --tail 10
# Expected: no errors, shows "renewed lease"
```

---

## Related Documentation

- [SSOT: IaC Runner](../../docs/ssot/bootstrap.iac_runner.md) - Comprehensive reference
- [SSOT: Pipeline](../../docs/ssot/ops.pipeline.md) - CI/CD workflows
- [GitHub Workflows](../../.github/workflows/) - platform-staging.yml, platform-production.yml
