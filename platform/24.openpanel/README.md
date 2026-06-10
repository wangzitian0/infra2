# OpenPanel

> **Purpose**: Open-source product analytics platform (Mixpanel alternative) with shared Postgres, Redis, and ClickHouse storage.

## Overview

OpenPanel is a lightweight product analytics tool for tracking user actions and analyzing them through funnels, retention, and event tables. It runs on Dokploy and integrates with our existing database instances to minimize resource usage.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Traefik                                │
│          openpanel.{domain} (Root) → op-dashboard           │
│          openpanel.{domain}/api/*  → Strip /api → op-api    │
└─────────────────────────────────────────────────────────────┘
                               │
            ┌──────────────────┼──────────────────┐
            │                  │                  │
      ┌─────▼─────┐      ┌─────▼─────┐      ┌─────▼─────┐
      │ Postgres  │      │   Redis   │      │ClickHouse │
      └───────────┘      └───────────┘      └───────────┘
```

## Dependencies

| Service | Purpose |
|---------|---------|
| postgres | Dashboard configuration, user accounts and workspace meta-data |
| redis | Queue processing (BullMQ) and temporary cache |
| clickhouse | High-performance column store for raw analytics events |
| vault | Runtime secrets injection |

## Quick Start

```bash
# 1. Deploy (pre-provisions databases and sets up vault keys)
invoke openpanel.setup

# 2. Check status
invoke openpanel.status
```

## Configuration

### Secrets in Vault

Path: `secret/platform/{env}/openpanel`

| Key | Description |
|-----|-------------|
| `encryption_key` | 32-byte hex key for data encryption (auto-generated) |
| `resend_api_key` | Optional API key for Resend email notifications |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | 3000 (dashboard) / 3333 (api) | Container port bindings |
| `DASHBOARD_URL` | https://openpanel.{domain} | Public URL of the frontend |
| `API_URL` | https://openpanel.{domain}/api | Public URL of the API gateway |
| `OPENPANEL_ALLOW_REGISTRATION` | false | Disable public signups (except during initial setup) |

## Access

- **URL**: `https://openpanel{ENV_DOMAIN_SUFFIX}.{INTERNAL_DOMAIN}`
- **API Endpoint**: `https://openpanel{ENV_DOMAIN_SUFFIX}.{INTERNAL_DOMAIN}/api`

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

# Verify clickhouse is running
invoke clickhouse.status

# Check clickhouse database exists
ssh root@{VPS_HOST} docker exec platform-clickhouse clickhouse-client -q 'show databases' | grep openpanel
```

## References

- [OpenPanel Self-Hosting Documentation](https://openpanel.dev/docs/self-hosting)
- [OpenPanel SSOT](../../docs/ssot/platform.openpanel.md)
- [Platform README](../README.md)
