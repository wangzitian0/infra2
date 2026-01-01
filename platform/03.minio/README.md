# MinIO

> **Purpose**: S3-compatible object storage for platform services.

## Quick Start

```bash
# Deploy
invoke minio.setup

# Check status
invoke minio.status
```

## Architecture

Uses vault-init pattern:
- Secrets stored in Vault (`secret/platform/production/minio`)
- Fetched at container runtime via vault-agent sidecar
- No secrets in Dokploy env vars or disk

## Ports

| Port | Purpose |
|------|---------|
| 9000 | S3 API |
| 9001 | Console |

## Vault Secrets

| Key | Description |
|-----|-------------|
| `root_user` | Admin username (default: admin) |
| `root_password` | Admin password |

## Data

- **Path**: `/data/platform/minio`
- **Persistence**: Host volume mount

## References

- [Platform README](../README.md)
- [MinIO Docs](https://min.io/docs/minio/linux/index.html)
