# Finance Report Redis

> **Purpose**: Dedicated Redis instance for Finance Report application caching and sessions.

## Overview

- **Port**: 6379 (internal only, no public exposure)
- **Container**: `finance_report-redis${ENV_SUFFIX}`
- **Persistence**: RDB snapshots every 60s if at least 1 key changed

## Secrets (Vault)

Secrets stored at: `secret/data/finance_report/<env>/redis`

| Key | Description |
|-----|-------------|
| `PASSWORD` | Redis authentication password |

## Quick Start

```bash
# Deploy
invoke finance_report.redis.setup

# Check status
invoke finance_report.redis.status

# Connect (from within Docker network)
docker exec -it finance_report-redis redis-cli -a <password>
```

## Connection String

For application use (internal network):
```
redis://:<password>@finance_report-redis${ENV_SUFFIX}:6379/0
```

## Data Path

```
/data/finance_report/redis${ENV_SUFFIX}/
```

## References

- [Platform Redis](../../../platform/02.redis/README.md) - Pattern reference
- [Vault Integration](../../../docs/ssot/db.vault-integration.md)
