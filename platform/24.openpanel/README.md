# OpenPanel

> **Purpose**: Open-source product analytics platform (Mixpanel alternative). Reuses shared Postgres + Redis; runs a dedicated, version-matched ClickHouse.

## Overview

OpenPanel is a lightweight product analytics tool for tracking user actions and analyzing them through funnels, retention, and event tables. It runs on Dokploy using the official upstream images `lindesvard/openpanel-{api,dashboard,worker}:2`.

> **Why a dedicated ClickHouse?** The shared `platform-clickhouse` is pinned to 25.5 by SigNoz (its owner — SigNoz v0.128 still ships 25.5.6), while OpenPanel v2 requires ClickHouse 25.10 (`DateTime64` TTL + newer query settings). The requirements are mutually exclusive and the shared instance cannot be upgraded without breaking SigNoz, so OpenPanel runs its own `op-ch` (25.10). See [SSOT](../../docs/ssot/platform.openpanel.md).

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Traefik                                │
│        openpanel.{domain} (Root)   → op-dashboard:3000      │
│        openpanel.{domain}/api/* → Strip /api → op-api:3000  │
└─────────────────────────────────────────────────────────────┘
                               │
        ┌──────────────────────┼─────────────────────┐
        │                      │                     │
  ┌─────▼─────┐  ┌─────▼─────┐  ┌─────▼─────┐  ┌──────▼──────┐
  │ Postgres  │  │   Redis   │  │   op-ch   │  │ vault-agent │
  │ (shared)  │  │ (shared)  │  │(dedicated)│  │  (secrets)  │
  └───────────┘  └───────────┘  └───────────┘  └─────────────┘
```

## Dependencies

| Service | Purpose |
|---------|---------|
| postgres (shared `platform-postgres`) | Dashboard configuration, user accounts and workspace meta-data |
| redis (shared `platform-redis`, DB 3) | Queue processing (BullMQ) and temporary cache |
| op-ch (dedicated ClickHouse 25.10) | High-performance column store for raw analytics events |
| vault | Runtime secrets injection |

## Quick Start

```bash
# 1. Deploy (pre-provisions databases and sets up vault keys)
python -m tools.deploy_v2 --service platform/openpanel --type prod --iac-ref vX.Y.Z --domain zitian.party --code-reviewed

# 2. Check status
invoke openpanel.status
```

## Configuration

### Secrets in Vault

Path: `secret/platform/{env}/openpanel`

| Key | Description |
|-----|-------------|
| `cookie_secret` | Session/cookie signing secret (auto-generated) |
| `resend_api_key` | Optional API key for Resend email notifications |

Postgres (`root_password`) and Redis (`password`) are read from their own Vault
paths and rendered into `DATABASE_URL`/`DATABASE_URL_DIRECT`/`REDIS_URL`.

### Environment Variables (upstream contract)

Secrets are rendered to `/secrets/.env` by vault-agent; non-secrets are set in
compose `environment:`.

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` / `DATABASE_URL_DIRECT` | (vault) | Shared Postgres `openpanel` DB |
| `REDIS_URL` | (vault) | Shared Redis DB 3 |
| `CLICKHOUSE_URL` | `http://op-ch:8123/openpanel` | Dedicated ClickHouse |
| `COOKIE_SECRET` | (vault) | Session signing secret |
| `DASHBOARD_URL` | `https://openpanel.{domain}` | Public URL of the frontend |
| `API_URL` | `https://openpanel.{domain}/api` | Public URL of the API gateway |
| `ALLOW_REGISTRATION` | `false` | Public signups (set `true` only for first-account creation) |

## Access

- **URL**: `https://openpanel{ENV_DOMAIN_SUFFIX}.{INTERNAL_DOMAIN}`
- **API Endpoint**: `https://openpanel{ENV_DOMAIN_SUFFIX}.{INTERNAL_DOMAIN}/api`

## Monitoring

`deploy.py` declares the probes that `platform/12.alerting` runs from every
environment (OpenPanel is `prod_only`, so the targets carry no suffix). Severity
follows [ops.observability §3/§5](../../docs/ssot/ops.observability.md#3-告警分级-severity):

| Probe | Severity | Why |
|---|---|---|
| `openpanel-api-http` | `error` (P1) | `/track` unreachable loses events; it is also the round-trip's cascade root |
| `openpanel-roundtrip` | `error` (P1) | a `/track` event that never reaches `op-ch` is lost (#726: 17 h of `NOSCRIPT` with a green `/healthcheck`) |
| `openpanel-worker-http` | `warning` (P2) | a worker that stops persisting pages P1 through the round-trip |
| `openpanel-dashboard-http` | `warning` (P2) | the UI; no data at risk |

## Troubleshooting

### Health Check Failed

```bash
# Check container logs
ssh root@{VPS_HOST} docker logs platform-openpanel-api
ssh root@{VPS_HOST} docker logs platform-openpanel-dashboard

# Check secrets are rendered
ssh root@{VPS_HOST} docker exec platform-openpanel-vault-agent cat /vault/secrets/.env
```

### Database Connection Issues

```bash
# Verify postgres is running
invoke postgres.status

# Verify the dedicated ClickHouse is up and has the openpanel DB
ssh root@{VPS_HOST} docker exec platform-openpanel-ch clickhouse-client -q 'show databases' | grep openpanel
```

## References

- [OpenPanel Self-Hosting Documentation](https://openpanel.dev/docs/self-hosting)
- [OpenPanel SSOT](../../docs/ssot/platform.openpanel.md)
- [Platform README](../README.md)
