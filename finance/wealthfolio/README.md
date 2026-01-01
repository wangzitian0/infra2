# Wealthfolio

> **Purpose**: Private investment portfolio tracker

## Overview

[Wealthfolio](https://wealthfolio.app/) is an open-source, private portfolio tracker that runs entirely locally.

- **Image**: `afadil/wealthfolio:latest`
- **Port**: 8088
- **Domain**: `wealth.${INTERNAL_DOMAIN}`
- **Project**: `finance`

## Quick Start

```bash
# Deploy
invoke wealthfolio.setup

# Check status
invoke wealthfolio.status
```

## Data Persistence

| Path | Description |
|------|-------------|
| `/data/finance/wealthfolio/wealthfolio.db` | SQLite database |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `WF_SECRET_KEY` | 32-byte base64 key for encryption (from Vault) |
| `WF_AUTH_PASSWORD_HASH` | Admin auth password hash (from Vault) |
| `WF_LISTEN_ADDR` | Bind address (0.0.0.0:8088) |
| `WF_DB_PATH` | Database path (/data/wealthfolio.db) |

## References

- [Wealthfolio GitHub](https://github.com/afadil/wealthfolio)
- [Docker Hub](https://hub.docker.com/r/afadil/wealthfolio)
