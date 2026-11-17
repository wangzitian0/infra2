# Free Gateway

> **Purpose**: Ingress proxy gateway service for secure tunnel access (`platform/free`).

## Overview

- **Service**: `free`
- **Deployer**: `FreeDeployer` (`platform/25.free/deploy.py`)
- **Port**: Container port `10000` mapped to host TLS port `443` through Traefik.
- **Protocol**: VLESS with WebSocket transport over TLS.
- **Routing**: `subdomain = None` (Traefik routing rules are configured in `compose.yaml`).

## Architecture and Security

- **UUID and Secret Path**: The deployer manages an RFC 4122 UUID and secret path at `${DATA_PATH}/config.json`.
- **Host drift**: Content in the host `config.json` that is not in `config.json.tmpl` is drift. The deploy overwrites it and keeps only the uuid and path.
- **Failed read**: Only a confirmed absent host file creates a new uuid and path. If the SSH read fails or returns no output, the deploy stops before any upload.
- **Empty file**: A present but empty host file also stops the deploy. Repair or remove the file on the host, then redeploy.
- **Exemptions**: Probes are exempt (`Exemption(check_id="probes")`) because the endpoint uses a private secret path.
- **Backup Strategy**: Uses `BackupFacet(method="config_archive")` to archive configuration state.
- **File Permissions**: Owner UID `0` and GID `0` with file permissions `0600`.

## Quick Start

```bash
# Deploy to production (CLI)
python -m tools.deploy_v2 --service platform/free --type prod --iac-ref vX.Y.Z --domain zitian.party --staging-validated --code-reviewed

# Deploy to production (GitHub Actions workflow_dispatch)
gh workflow run deploy.yml \
  -f service=platform/free \
  -f type=prod \
  -f iac_ref=vX.Y.Z \
  -f staging_validated=true \
  -f code_reviewed=true

# Deploy to staging
python -m tools.deploy_v2 --service platform/free --type staging --iac-ref vX.Y.Z --domain zitian.party
```

## Credential Rotation (UUID & Path)

When `/data/platform/free/config.json` is absent on the host, `FreeDeployer.verify_still_in_service` blocks the unchanged-config skip and forces a full redeploy that creates a fresh UUID and secret path (#1615).

1. Move the active host config aside on the VPS: `mv /data/platform/free/config.json /data/platform/free/config.json.bak`.
2. Run `deploy.yml` with `service=platform/free`, `type=prod`, `iac_ref=<latest-tag>`, `staging_validated=true`, and `code_reviewed=true` (after staging validation).
3. Inspect the new client connection details with `FREE_SHOW_SECRETS=1` and remove the `.bak` file once clients switch over.

## Client Connection Information

Client connection details (VLESS URL, UUID, and path) are printed during deployment (`post_compose`).
Set `FREE_SHOW_SECRETS=1` (and optionally `FREE_CLIENT_INFO_PATH`) in your environment during deploy:
```bash
FREE_SHOW_SECRETS=1 FREE_CLIENT_INFO_PATH=/tmp/free-client.md python -m tools.deploy_v2 --service platform/free --type prod ...
```
To inspect the running service without secret exposure, run `invoke free.status`.
