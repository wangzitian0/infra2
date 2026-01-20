# IaC Runner

GitOps-style webhook service that syncs infrastructure when main branch changes.

## How It Works

```
┌─────────────┐     webhook      ┌──────────────┐     invoke sync   ┌─────────────┐
│   GitHub    │ ──────────────▶ │  IaC Runner  │ ─────────────────▶│  Services   │
│  (push)     │                  │  (container) │                   │  (Dokploy)  │
└─────────────┘                  └──────────────┘                   └─────────────┘
                                         │
                                         ▼
                                  GitHub Commit
                                  Status API
```

1. Developer pushes to `main` branch
2. GitHub sends webhook to `https://iac.{domain}/webhook`
3. IaC Runner parses changed files to identify affected services
4. Runs `invoke {service}.sync` for each changed service
5. Sync task compares config hash and only redeploys if changed
6. Updates GitHub commit status (✅ success / ❌ failure)

## Key Features

### Idempotency
The `sync` task uses config hashing:
- Computes SHA256 of `compose.yaml + env vars`
- Stores hash as `IAC_CONFIG_HASH` in Dokploy env
- Skips deploy if hash matches (no changes)

### Auto-Discovery
Services are discovered automatically by scanning `*/deploy.py` files:
- `platform/{nn}.{service}/` → `{service}.sync`
- `finance_report/finance_report/{nn}.{service}/` → `fr-{service}.sync`

No manual `SERVICE_TASK_MAP` maintenance required!

### Concurrency Safety
File-based locking (`/workspace/.sync.lock`) prevents concurrent git operations.

### GitHub Integration
Commit status updates provide real-time feedback on deployment success/failure.

## Setup

### 1. Generate Webhook Secret

```bash
openssl rand -hex 32
```

Store in Vault: `secret/bootstrap/production/iac-runner` → `WEBHOOK_SECRET`

### 2. Generate GitHub Token

Create a GitHub Personal Access Token with `repo:status` scope.

Store in Vault: `secret/bootstrap/production/iac-runner` → `GITHUB_TOKEN`

### 3. Configure GitHub Webhook

1. Go to repo Settings → Webhooks → Add webhook
2. Payload URL: `https://iac.{your-domain}/webhook`
3. Content type: `application/json`
4. Secret: (the secret you generated)
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

## Service Mapping (Auto-Discovered)

| Changed Path | Invoke Task |
|--------------|-------------|
| `platform/01.postgres/*` | `postgres.sync` |
| `finance_report/finance_report/10.app/*` | `fr-app.sync` |
| `libs/*` | All discovered services |
| `bootstrap/*` | Skipped (manual only) |

**Manual Exclusions**:
- `bootstrap/vault` → None (too risky)
- `bootstrap/1password` → None
- `bootstrap/iac-runner` → None (avoid self-restart)

## Security Considerations

- HMAC signature verification for all requests
- Read-only docker socket mount
- No write access to host filesystem (except workspace)
- Bootstrap services excluded from auto-sync
- GitHub token limited to `repo:status` scope

## Testing

E2E tests are located in `e2e_regressions/tests/bootstrap/test_iac_runner.py`:

```bash
cd e2e_regressions
export IAC_RUNNER_WEBHOOK_SECRET="your-secret"
uv run pytest tests/bootstrap/test_iac_runner.py -v
```
