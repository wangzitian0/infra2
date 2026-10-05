# Canary Todo (Infrastructure Verification Tool)

Canary service deployed at `todo.zitian.party` for verifying and self-proving all platform infrastructure runtime capabilities.

## Overview

- **Service**: todo
- **Port**: 8000 (container) -> 443 (Traefik)
- **Subdomain**: `todo` (`todo.zitian.party`)
- **Deployer**: `TodoDeployer` (`platform/30.todo/deploy.py`)

## Validated Capabilities

The service actively validates platform infrastructure components via physical probes:
1. **Postgres** (`platform/01.postgres`): Reads/writes relational state.
2. **Redis** (`platform/02.redis`): Cache ping and distributed lock probe.
3. **S3 Storage** (`platform/03.s3`): Object storage health and presigned operations.
4. **SigNoz** (`platform/11.signoz`): OTel collector trace ingest health.
5. **OpenPanel** (`platform/24.openpanel`): Analytics API healthcheck.
6. **Authentik** (`platform/10.authentik`): SSO health & forward-auth integration.

## Endpoints

- **Health Probe (Public)**: `GET /api/health` (HTTP 200 JSON status)
- **Canary Status (Public)**: `GET /api/canary/status` (Full infrastructure capability matrix)
- **Interactive Web UI (SSO Protected)**: `GET /` (Authentik forward-auth protected Todo dashboard)
