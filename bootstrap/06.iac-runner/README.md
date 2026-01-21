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

## Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/webhook` | POST | GitHub webhook receiver |
| `/sync` | POST | Manual sync trigger |

### Manual Sync

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

## Service Mapping

| Changed Path | Invoke Task |
|--------------|-------------|
| `platform/01.postgres/*` | `postgres.sync` |
| `finance_report/finance_report/10.app/*` | `fr-app.sync` |
| `libs/*` | All services (full sync) |
| `bootstrap/*` | Skipped (manual only) |

## Security Considerations

- HMAC signature verification for all requests
- Read-only docker socket mount
- No write access to host filesystem (except workspace)
- Bootstrap services excluded from auto-sync
