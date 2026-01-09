# Activepieces

> **Purpose**: Low-code automation platform (Zapier/n8n alternative) with SSO protection.

## Overview

Activepieces is a self-hosted automation platform that allows building workflows to connect different apps and services. It's protected by Authentik SSO for secure access.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Traefik                                │
│     automate.{domain} → Authentik ForwardAuth → Activepieces│
└─────────────────────────────────────────────────────────────┘
                              │
                    ┌─────────┴─────────┐
                    │                   │
              ┌─────▼─────┐      ┌──────▼─────┐
              │ Postgres  │      │   Redis    │
              └───────────┘      └────────────┘
```

## Dependencies

| Service | Purpose |
|---------|---------|
| postgres | Workflow and user data storage |
| redis | Queue and caching |
| authentik | SSO authentication |
| vault | Secrets management |

## Quick Start

```bash
# 1. Deploy (requires dependencies to be up)
invoke activepieces.setup

# 2. Configure SSO protection
invoke authentik.shared.create-proxy-app \
  --name=Activepieces \
  --slug=activepieces \
  --external-host=https://automate.{domain} \
  --internal-host=platform-activepieces

# 3. Check status
invoke activepieces.status
```

## Configuration

### Secrets in Vault

Path: `secret/platform/{env}/activepieces`

| Key | Description |
|-----|-------------|
| `encryption_key` | 32-char hex key for data encryption |
| `jwt_secret` | JWT signing secret |
| `frontend_url` | Public URL (auto-generated) |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AP_ENVIRONMENT` | prod | Environment mode |
| `AP_SANDBOX_RUN_TIME_SECONDS` | 600 | Max flow execution time |
| `AP_PIECES_SYNC_MODE` | OFFICIAL_AUTO | Pieces update mode |
| `AP_TELEMETRY_ENABLED` | false | Disable telemetry |

## Access

- **URL**: `https://automate{ENV_DOMAIN_SUFFIX}.{INTERNAL_DOMAIN}`
- **Auth**: Authentik SSO (admins group)

## Troubleshooting

### Health Check Failed

```bash
# Check container logs
ssh root@{VPS_HOST} docker logs platform-activepieces

# Check secrets are rendered
ssh root@{VPS_HOST} docker exec platform-activepieces-vault-agent cat /vault/secrets/.env
```

### Database Connection Issues

```bash
# Verify postgres is running
invoke postgres.status

# Check database exists
ssh root@{VPS_HOST} docker exec platform-postgres psql -U postgres -c '\l' | grep activepieces
```

## References

- [Activepieces Documentation](https://www.activepieces.com/docs)
- [Platform README](../README.md)
- [Vault Integration SSOT](../../docs/ssot/db.vault-integration.md)
- [SSO SSOT](../../docs/ssot/platform.sso.md)
