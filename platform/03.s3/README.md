# Platform S3-Compatible Object Storage

> **Purpose**: Generic S3-compatible object storage service for platform and application services (`platform/s3`).
> Designed with storage engine neutrality: currently powered by **RustFS** (Apache 2.0, memory-safe Rust implementation) as the default engine, while maintaining dynamic backend compatibility for future alternatives (Garage, SeaweedFS, Ceph, etc.).

## Quick Start

```bash
# Deploy staging
python -m tools.deploy_v2 --service platform/s3 --type staging --iac-ref vX.Y.Z --domain zitian.party

# Check status
invoke s3.status
```

## Architecture

- **Capability**: Standard AWS S3 API + Web Console.
- **Current Engine**: [RustFS](https://github.com/rustfs/rustfs) with embedded `mc` CLI for administration compatibility.
- **Vault-init pattern**:
  - Secrets stored in Vault (`secret/platform/<env>/minio`).
  - Fetched at container runtime via `vault-agent` sidecar.
  - Rendered into tmpfs in-memory volume `/secrets/.env` and exported as generic `S3_*` and engine-specific `RUSTFS_*`/`MINIO_*` variables.
  - No secrets in Dokploy env vars or on unencrypted disk.
- **Downstream Compatibility**:
  - Container maintains Docker network aliases: `platform-s3` and `platform-rustfs`.
  - Zero code modifications required for existing downstream consumers (`truealpha`, `finance_report`).
  - Supports standard AWS S3 SDK, boto3, `mc`, and `rc`.

## Files

| File | Purpose |
|------|---------|
| `compose.yaml` | x-infra2 source; the compiler adds the vault-agent sidecar (#1822 step B) |
| `Dockerfile` | RustFS 1.0.0 with `mc`; CI publishes it as `ghcr.io/wangzitian0/infra2-s3` (`common/deploy/platform_images.py`) |
| `deploy.py` | S3 deployer with vault-init |
| `env.manifest.json` | Secret keys; the vault-agent template renders from it |
| `shared_tasks.py` | `status` and `create_app_bucket` tasks |
| `vault-policy.hcl` | Read-only policy for the S3 secret |

The compiled text has no `vault-agent.hcl` or `secrets.ctmpl` file in git. Read it with
`python -m tools.compose_compile print platform/03.s3/compose.yaml`.

## Domains & Ports

| Port | Protocol / Purpose | Staging Domain | Production Domain |
|------|--------------------|----------------|-------------------|
| **9000** | S3 API | `https://s3-staging.zitian.party` | `https://s3.zitian.party` |
| **9001** | Web Console | `https://s3-console-staging.zitian.party`<br>*(aliases: rustfs-staging, minio-staging)* | `https://s3-console.zitian.party`<br>*(aliases: rustfs, minio)* |
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

## Legacy Compatibility Aliases

The platform keeps compatibility aliases during the transition from MinIO to RustFS:
- **Python Module Imports**: Retired. Consumers import canonical `platform.03.s3.shared`.
- **Docker Network**: Retired. The `platform-minio` alias is gone; use `platform-s3`.
- **Vault Secret Paths**: Secrets stay at `secret/platform/<env>/minio` to avoid secret migrations.
- **Backup Identifiers**: Automated backup processes accept `platform/minio-*` archive names during the retention window.

## References

- [RustFS GitHub](https://github.com/rustfs/rustfs)
- [Platform README](../README.md)
