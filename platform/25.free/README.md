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
- **Exemptions**: Probes are exempt (`Exemption(check_id="probes")`) because the endpoint uses a private secret path.
- **Backup Strategy**: Uses `BackupFacet(method="config_archive")` to archive configuration state.
- **File Permissions**: Owner UID `0` and GID `0` with file permissions `0600`.

## Quick Start

```bash
# Deploy to production
python -m tools.deploy_v2 --service platform/free --type prod --iac-ref vX.Y.Z --domain zitian.party --code-reviewed

# Deploy to staging
python -m tools.deploy_v2 --service platform/free --type staging --iac-ref vX.Y.Z --domain zitian.party
```

## Client Connection Information

Client connection details (VLESS URL, UUID, and path) are printed during deployment (`post_compose`).
Set `FREE_SHOW_SECRETS=1` in your environment during deploy:
```bash
FREE_SHOW_SECRETS=1 python -m tools.deploy_v2 --service platform/free --type prod ...
```
To inspect the running service without secret exposure, run `invoke free.status`.
