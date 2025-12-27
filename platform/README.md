# Platform Module

Platform services that require persistent state and complex orchestration. Unlike `bootstrap` (core infra, low-dependency), `platform` hosts stateful applications with dependencies on databases, caches, and the Vault secret store.

## Architecture Overview

```text
┌─────────────────────────────────────────────────────────┐
│           Platform Admin                                 │
│  - Platform PG Root Access                               │
│  - Vault Root Token                                      │
└────────────────┬────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────┐
│          Module 1: Secret Generator                      │
│  - Generate credentials for downstream services          │
│  - Create DB users in Platform PG                        │
│  - Generate app configuration (env vars)                 │
└────────────────┬────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────┐
│          Module 2: Vault Manager                         │
│  - Create Vault path structure                           │
│  - Write secrets and config                              │
│  - Create Policies (per-service isolation)               │
│  - Generate AppRoles (distributed to apps)               │
└────────────────┬────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────┐
│          Module 3: App Orchestrator                      │
│  - Generate docker-compose.yml (with Init Container)     │
│  - Inject AppRole credentials (role-id/secret-id)        │
│  - Deploy to Dokploy                                     │
└────────────────┬────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────┐
│          Module 4: Application Runtime                   │
│  - Init Container fetches config from Vault              │
│  - Start app (with merged config)                        │
│  - Connect to databases                                  │
└─────────────────────────────────────────────────────────┘
```

## Directory Numbering Convention

| Range     | Category          | Examples                              |
|-----------|-------------------|---------------------------------------|
| `01-09`   | **Databases**     | `01.postgres`, `02.redis`, `03.clickhouse` |
| `10-19`   | **Auth & Gateway**| `10.authentik`, `11.kong`             |
| `20-29`   | **Observability** | `20.signoz`, `21.grafana`             |
| `30+`     | **Applications**  | `30.openpanel`, `31.myapp`            |

## Folder Structure

```
platform/
├── README.md                           # This file
├── 01.postgres/                         # Platform PG (shared by platform apps)
│   ├── compose.yaml
│   ├── tasks.py
│   └── README.md
├── 02.redis/                            # Shared Redis cache
│   ├── compose.yaml
│   ├── tasks.py
│   └── README.md
├── 10.authentik/                        # Identity Provider (SSO)
│   ├── compose.yaml                     # Main app + init container
│   ├── tasks.py                         # Vault secret setup, deploy automation
│   └── README.md
└── _templates/                          # Reusable templates
    ├── vault-init.Dockerfile            # Init container for pulling secrets
    └── app-compose.template.yaml        # Base compose for Vault-aware apps
```

## Vault Directory Structure

Secrets are organized by layer with strict policy isolation.

```
secret/
├── _platform/                  # Admin-only access
│   ├── postgres-root          # Platform PG root password
│   └── vault-master-key       # Vault unsealing keys
│
├── _global/                   # All apps can read
│   ├── company-name
│   ├── log-level
│   └── timezone
│
├── infrastructure/            # Database layer
│   ├── postgres/
│   │   ├── admin
│   │   ├── app-user
│   │   └── readonly
│   ├── redis/
│   │   └── password
│   ├── clickhouse/
│   │   └── admin
│   └── arangodb/
│       └── root
│
├── services/                  # Application layer
│   ├── authentik/
│   │   ├── secret-key
│   │   └── db-connection
│   ├── signoz/
│   │   ├── db-connection
│   │   └── api-keys
│   └── openpanel/
│       ├── db-connection
│       └── api-keys
│
└── prod/                      # Environment overrides (optional)
    ├── infrastructure/
    └── services/
```

## Init Container Pattern

Every platform application uses an Init Container to fetch secrets from Vault before starting.

### Compose Pattern

```yaml
services:
  vault-init:
    image: vault:1.15
    command: |
      sh -c "
        vault login -method=approle role_id=$VAULT_ROLE_ID secret_id=$VAULT_SECRET_ID &&
        vault kv get -format=json secret/services/authentik > /shared-config/secrets.json
      "
    volumes:
      - config-volume:/shared-config
    environment:
      VAULT_ADDR: http://vault:8200
      VAULT_ROLE_ID: ${VAULT_ROLE_ID}
      VAULT_SECRET_ID: ${VAULT_SECRET_ID}

  app:
    image: ghcr.io/example/myapp:latest
    depends_on:
      vault-init:
        condition: service_completed_successfully
    volumes:
      - config-volume:/config:ro
    entrypoint: ["/bin/sh", "-c", "source /config/env.sh && exec myapp"]

volumes:
  config-volume:
```

## Tooling Split

| Purpose                  | Tooling              | Location                          |
|--------------------------|----------------------|-----------------------------------|
| Secret generation        | Python (`secrets`)   | `tasks.py` in each module         |
| DB user creation (PG)    | Python (`psycopg2`)  | `platform/01.postgres/tasks.py`   |
| Vault path/policy setup  | Python (`hvac`)      | Shared library or per-app tasks   |
| AppRole generation       | Python (`hvac`)      | `platform/_lib/vault_manager.py`  |
| Compose generation       | Jinja2 templates     | `platform/_templates/`            |
| Compose configuration    | YAML                 | `compose.yaml` per service        |
| Deployment orchestration | Invoke + Dokploy API | `tasks.py` per service            |

## Onboarding Templates

Templates for new apps are provided in `docs/onboarding/`:

| Template                     | Description                                    |
|------------------------------|------------------------------------------------|
| `04.secrets.md`              | How to request & use secrets from Vault        |
| `platform-app-template.md`   | (TODO) Step-by-step guide for new platform app |

## Next Steps

- [ ] Create `01.postgres/` for shared Platform PostgreSQL
- [ ] Create `02.redis/` for shared cache
- [ ] Refactor `10.authentik/` to use Init Container pattern
- [ ] Implement `_lib/vault_manager.py` for AppRole automation
- [ ] Implement `_templates/vault-init.Dockerfile` for Init Container

---

> **SSOT Reference**: [platform.secrets.md](../docs/ssot/platform.secrets.md)
