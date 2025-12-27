# Platform Module

> **Purpose**: Stateful applications and shared infrastructure components that depend on databases, caches, and Vault-managed secrets.

## Module Structure

This layer is organized by service category using a numbering convention:

| Range     | Category          | Examples                              |
|-----------|-------------------|---------------------------------------|
| `01-09`   | **Databases**     | `01.postgres`, `02.redis`, `03.clickhouse` |
| `10-19`   | **Auth & Gateway**| `10.authentik`, `11.kong`             |
| `20-29`   | **Observability** | `20.signoz`, `21.grafana`             |
| `30+`     | **Applications**  | `30.openpanel`, `31.myapp`            |

## Current Components

```
platform/
├── README.md                    # This file
├── 10.authentik/                # Identity Provider (SSO)
│   ├── compose.yaml             
│   ├── tasks.py                 
│   └── README.md
└── _templates/                  # Reusable templates (TODO)
    └── vault-init.Dockerfile
```

## Design Principles

1. **Shared Infrastructure First**: Common databases (PG, Redis) are deployed in 01-09 range
2. **Vault-Backed Secrets**: All apps pull secrets via Init Containers from Vault
3. **Modular Deployment**: Each service can be deployed independently via `invoke <service>.setup`
4. **Layer Isolation**: Services use Vault AppRole for strict secret access control

## Deployment Pattern

All platform services follow this pattern:

1. **Prepare**: Create VPS directories and database users
2. **Vault Setup**: Create Vault paths, policies, and AppRoles  
3. **Deploy**: Generate compose with Init Container and deploy to Dokploy
4. **Verify**: Check service health and connectivity

See individual service READMEs for specific instructions.

## References

- **Architecture Deep-Dive**: [docs/ssot/platform.secrets.md](../docs/ssot/platform.secrets.md)
- **Developer Guide**: [docs/onboarding/04.secrets.md](../docs/onboarding/04.secrets.md)
- **Vault Integration**: [docs/ssot/db.vault-integration.md](../docs/ssot/db.vault-integration.md)

## Next Steps

- [ ] Implement `01.postgres` (shared Platform PostgreSQL)
- [ ] Implement `02.redis` (shared cache)
- [ ] Refactor `10.authentik` to use Init Container pattern
- [ ] Create `_templates/vault-init.Dockerfile`
