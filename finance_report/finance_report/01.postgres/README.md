# Finance Report PostgreSQL

> **Purpose**: Dedicated PostgreSQL 16 instance for Finance Report application.

## Overview

- **Database**: `finance_report`
- **User**: `postgres`
- **Port**: 5432 (internal only, no public exposure)
- **Container**: `finance_report-postgres${ENV_SUFFIX}`

## Secrets (Vault)

Secrets stored at: `secret/data/finance_report/<env>/postgres`

| Key | Description |
|-----|-------------|
| `POSTGRES_PASSWORD` | PostgreSQL superuser password |

## Quick Start

```bash
# Deploy
invoke finance_report.postgres.setup

# Check status
invoke finance_report.postgres.status

# Connect (from within Docker network)
docker exec -it finance_report-postgres psql -U postgres -d finance_report
```

## Connection String

For application use (internal network):
```
postgresql://postgres:<password>@finance_report-postgres${ENV_SUFFIX}:5432/finance_report
```

## Data Path

```
/data/finance_report/postgres${ENV_SUFFIX}/
```

## Backup

```bash
# Manual backup
docker exec finance_report-postgres pg_dump -U postgres finance_report > backup.sql

# Restore
docker exec -i finance_report-postgres psql -U postgres finance_report < backup.sql
```

## References

- [Platform PostgreSQL](../../../platform/01.postgres/README.md) - Pattern reference
- [Vault Integration](../../../docs/ssot/db.vault-integration.md)
