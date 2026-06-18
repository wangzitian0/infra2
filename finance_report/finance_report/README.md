# Finance Report Application Layer

> **Purpose**: Stateful services for Finance Report using vault-init pattern.

## Structure

| Range | Category | Services |
|-------|----------|----------|
| `01` | **Database** | PostgreSQL 16 (dedicated) |
| `02` | **Cache** | Redis (dedicated) |
| `10` | **Application** | Backend + Frontend |

## Service Directory

```
finance_report/
├── 01.postgres/
│   ├── compose.yaml       # PostgreSQL with vault-agent sidecar
│   ├── deploy.py          # PostgresDeployer
│   ├── shared_tasks.py    # status() check
│   ├── vault-agent.hcl    # Vault agent config
│   ├── vault-policy.hcl   # Vault policy
│   ├── secrets.ctmpl      # Secrets template
│   └── README.md
├── 02.redis/
│   ├── compose.yaml       # Redis with vault-agent sidecar
│   ├── deploy.py          # RedisDeployer
│   ├── shared_tasks.py    # status() check
│   ├── vault-agent.hcl    # Vault agent config
│   ├── vault-policy.hcl   # Vault policy
│   ├── secrets.ctmpl      # Secrets template
│   └── README.md
└── 10.app/
    ├── compose.yaml       # Backend + Frontend with vault-agent
    ├── deploy.py          # AppDeployer
    ├── shared_tasks.py    # status() check
    ├── vault-agent.hcl    # Vault agent config
    ├── vault-policy.hcl   # Vault policy
    ├── secrets.ctmpl      # Secrets template
    └── README.md
```

## Deployment Order

```
postgres ─┐
          ├──► app
redis ────┘
```

## Prerequisites

1. **Vault ready**: `invoke vault.status` should return healthy
2. **MinIO ready**: `invoke minio.status` should return healthy
3. **Secrets written**: Secrets in `secret/finance_report/<env>/*`

## Quick Start

```bash
# Deploy all through deploy_v2 (in dependency order)
python -m tools.deploy_v2 --service finance_report/postgres --type staging --iac-ref vX.Y.Z --domain zitian.party
python -m tools.deploy_v2 --service finance_report/redis --type staging --iac-ref vX.Y.Z --domain zitian.party
python -m tools.deploy_v2 --service finance_report/app --type staging --version-ref vX.Y.Z --iac-ref vX.Y.Z --domain zitian.party

# Check status
invoke fr-postgres.shared.status
invoke fr-redis.shared.status
invoke fr-app.shared.status
```

## Environment Variables

Uses standard environment convention:
- `DEPLOY_ENV` selects target environment (default: `production`)
- `ENV_DOMAIN_SUFFIX` derived from `DEPLOY_ENV` (`""` for prod, `-<env>` for non-prod)
- `ENV_SUFFIX` for container/data isolation if needed

## References

- [Parent README](../README.md)
- [Platform README](../../platform/README.md) - Pattern reference
- [Vault Integration SSOT](../../docs/ssot/db.vault-integration.md)
