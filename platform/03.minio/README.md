# Platform Object Storage (RustFS / MinIO-Compatible)

> **Purpose**: S3-compatible object storage for platform and application services.
> Powered by **RustFS** (Apache 2.0, memory-safe Rust implementation) as a drop-in replacement for MinIO (#930).

## Quick Start

```bash
# Deploy staging
python -m tools.deploy_v2 --service platform/minio --type staging --iac-ref vX.Y.Z --domain zitian.party

# Check status
invoke minio.status
```

## Architecture

- **Engine**: [RustFS](https://github.com/rustfs/rustfs) with embedded `mc` CLI for administration compatibility.
- **Vault-init pattern**:
  - Secrets stored in Vault (`secret/platform/<env>/minio`).
  - Fetched at container runtime via `vault-agent` sidecar.
  - No secrets in Dokploy env vars or on unencrypted disk.
- **Downstream Compatibility**:
  - Container maintains Docker network alias `platform-minio` and `platform-rustfs`.
  - Supports standard AWS S3 SDK, boto3, `mc`, and `rc`.

## Domains & Ports

| Port | Protocol / Purpose | Staging Domain | Production Domain |
|------|--------------------|----------------|-------------------|
| **9000** | S3 API | `https://s3-staging.zitian.party` | `https://s3.zitian.party` |
| **9001** | Web Console | `https://rustfs-staging.zitian.party`<br>*(alias: minio-staging)* | `https://rustfs.zitian.party`<br>*(alias: minio)* |
| **19000/19001** | Host Loopback S3 | `127.0.0.1:19000` | `127.0.0.1:19001` |

## Vault Secrets

| Key | Description |
|-----|-------------|
| `root_user` | Admin username (default: admin) |
| `root_password` | Admin password |

## 1Password (Web UI)

- Item: `platform/minio/admin` (non-production: `platform/minio/admin-<env>`)
- `<env>` matches `DEPLOY_ENV` (e.g., `staging`), avoiding `-` or `/` in the env name.

## Data & Permissions

- **Path**: `${DATA_PATH}` (staging uses `/data/platform/minio-staging`, prod uses `/data/platform/minio`)
- **Persistence**: Host volume mount
- **User/Group**: UID `10001`, GID `10001` (managed automatically by `Deployer._prepare_dirs`)

## References

- [RustFS GitHub](https://github.com/rustfs/rustfs)
- [Platform README](../README.md)
