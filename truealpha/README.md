# TrueAlpha Deployment

> **Purpose**: IaC layer for the TrueAlpha application — deploy config, secrets wiring,
> and health checks. Application source lives in the separate
> [`repos/truealpha`](../repos/truealpha) workspace submodule; this directory only
> owns the deploy coordinate, not the app code.

## Architecture

```
truealpha/
├── truealpha/                # Application layer
│   ├── 01.postgres/         # Dedicated PostgreSQL
│   ├── 10.app/               # LLM + web services
│   └── 20.data_engine/       # Dagster capture/ingestion
└── README.md                 # This file
```

## Dependencies

| Service | Depends On | Notes |
|---------|------------|-------|
| postgres | vault | Secrets from Vault (AppRole) |
| app | postgres, vault | `truealpha-llm` + `truealpha-web` containers |
| data_engine | postgres, vault | Dagster webserver + daemon |

## Quick Start

```bash
# Deploy database layer through deploy_v2
python -m tools.deploy_v2 --service truealpha/postgres --type staging --iac-ref vX.Y.Z --domain zitian.party

# Deploy application through deploy_v2
python -m tools.deploy_v2 --service truealpha/app --type staging --version-ref vX.Y.Z --iac-ref vX.Y.Z --domain zitian.party

# Verify
invoke ta-postgres.shared.status
invoke ta-app.shared.status
invoke ta-data-engine.shared.status
```

## Vault Secrets

Secrets are stored in Vault KV v2 (AppRole auth from day one — declared per
service as `SecretsFacet` on its Deployer, see `libs/service_facets.py`; the
audit inventory derives from those declarations, #542):

```
secret/data/truealpha/<env>/postgres
secret/data/truealpha/<env>/app
secret/data/truealpha/<env>/data_engine
```

## References

- **Source Code**: [github.com/wangzitian0/truealpha](https://github.com/wangzitian0/truealpha)
- **Project docs**: [Infra-017](../docs/project/Infra-017.truealpha_dagster_capture.md), [Infra-020](../docs/project/Infra-020.truealpha_production_datahub.md)
- **Finance Report README** (sibling app layer, same pattern): [finance_report/README.md](../finance_report/README.md)
