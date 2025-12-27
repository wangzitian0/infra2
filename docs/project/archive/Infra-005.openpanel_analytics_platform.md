# Infra-005: OpenPanel Analytics Platform Deployment

**Status**: Archived  
**Owner**: Infra  
**Legacy Source**: OpenPanel deployment plan (L2/L3 infra + L4 Helm)

## Summary
Prepare L2/L3 infrastructure and plan the L4 Helm deployment of OpenPanel with
Portal Gate authentication for SSO.

## PR Links
- PR #336: https://github.com/wangzitian0/infra/pull/336

## Change Log
- No dedicated change_log entry (plan document only).

## Git Commits (Backtrace)
- 0a2d311 feat(platform): add OpenPanel L2 infrastructure with Portal Gate authentication (#336)

## Legacy Plan (OpenPanel Deployment)

## 📊 Current Status (2025-12-22)

### ✅ Completed (L2 Infrastructure)
- **PostgreSQL User**: `openpanel` user created in L3 PostgreSQL
- **PostgreSQL Database**: Dedicated `openpanel` database (not shared with `app`)
- **ClickHouse User**: `openpanel` user created in L3 ClickHouse
- **ClickHouse Database**: `openpanel_events` for event storage
- **Vault Credentials**: Stored at `secret/data/openpanel`
- **Authentication**: OpenPanel will use **Portal Gate (OAuth2-Proxy)** for SSO (no native OIDC/SAML support)

### 🔄 In Progress (L4 Deployment)
OpenPanel **officially supports Kubernetes/Helm** deployment (unlike PostHog).

**Advantages over PostHog**:
- ✅ Active Helm chart maintenance
- ✅ Smaller resource footprint
- ✅ Cookie-free tracking (privacy-first)
- ✅ Built-in A/B testing
- ✅ Real-time dashboards

**Trade-offs**:
- ⚠️ Smaller community (vs PostHog)
- ⚠️ Fewer enterprise features (no Session Replay in open-source)

---

## 🔧 Deployment Method

### Kubernetes Helm Deployment (Official)
**Status**: Supported and actively maintained.

**Helm Chart**: https://openpanel.dev/docs/self-hosting/deploy-kubernetes

**Pros**:
- ✅ Official support
- ✅ Kubernetes-native
- ✅ Auto-scaling support
- ✅ Integrated with L2/L3 infrastructure

**Cons**:
- ⚠️ Requires configuration (not fully "ready-to-use")
- ⚠️ Need to configure external databases

**Resources**:
- [OpenPanel Self-Host Docs](https://openpanel.dev/docs/self-hosting/self-hosting)
- [Kubernetes Deployment Guide](https://openpanel.dev/docs/self-hosting/deploy-kubernetes)
- [GitHub Repository](https://github.com/Openpanel-dev/openpanel)

---

## 📋 Prepared Infrastructure (Ready to Use)

All database credentials are stored in Vault KV at `secret/data/openpanel`:

```json
{
  "postgres_host": "postgresql.data-staging.svc.cluster.local",
  "postgres_port": "5432",
  "postgres_user": "openpanel",
  "postgres_password": "<generated>",
  "postgres_database": "openpanel",

  "redis_host": "redis-master.data-staging.svc.cluster.local",
  "redis_port": "6379",
  "redis_password": "<from L3>",

  "clickhouse_host": "clickhouse.data-staging.svc.cluster.local",
  "clickhouse_port": "9000",
  "clickhouse_user": "openpanel",
  "clickhouse_password": "<generated>",
  "clickhouse_database": "openpanel_events",

  "saml_idp_entity_id": "https://sso.${INTERNAL_DOMAIN}",
  "saml_idp_sso_url": "https://sso.${INTERNAL_DOMAIN}/api/saml",
  "saml_idp_metadata": "https://sso.${INTERNAL_DOMAIN}/api/saml/metadata?application=built-in/openpanel-saml"
}
```

---

## 🎯 Next Steps

### Phase 1: Verify OpenPanel SAML Support
**Action**: Check if OpenPanel supports SAML natively.

**Investigation**:
- Review OpenPanel documentation for SSO/SAML configuration
- Check Helm chart values for authentication options
- If SAML is not supported, use OAuth2 Proxy as authentication gateway

### Phase 2: Deploy OpenPanel (L4)
**After SAML verification**:
1. Create `4.apps/3.openpanel.tf` with Helm deployment
2. Configure external databases (PostgreSQL, Redis, ClickHouse)
3. Set up Ingress with TLS: `https://openpanel.${internal_domain}`
4. Deploy via Atlantis: `atlantis apply -p apps`

### Phase 3: Configure Authentication

**Authentication Strategy: Portal Gate (OAuth2-Proxy)**

OpenPanel does **not support native OIDC or SAML**. Authentication will be handled by:
- **Portal Gate**: OAuth2-Proxy + Traefik ForwardAuth middleware
- **SSO Provider**: Casdoor (GitHub OAuth + Password login)
- **Pattern**: Same as Kubernetes Dashboard (see `docs/ssot/platform.auth.md`)

**Configuration Steps**:
1. Ensure `enable_portal_sso_gate=true` in L2 platform
2. Add OpenPanel Ingress with ForwardAuth annotations
3. Users authenticate via Casdoor before accessing OpenPanel
4. OpenPanel itself uses local auth (accounts synced manually or via API)

**Alternative**: If OpenPanel adds OIDC/SAML support in the future, migrate to native authentication.

---

## 📝 Technical Notes

### Database Schema Ownership
OpenPanel user has **OWNER** privileges on `openpanel` database:
```sql
-- Verified in L2 configuration
OWNER = openpanel  -- Can run migrations
```

### ClickHouse Event Storage
OpenPanel uses ClickHouse for high-volume event data:
```sql
-- Database: openpanel_events
-- User: openpanel (ALL privileges)
-- Tables: Created by OpenPanel migrations
```

### Authentication Architecture

**Pattern**: Portal Gate (OAuth2-Proxy)
- **Layer**: Traefik ForwardAuth middleware
- **SSO**: Casdoor (`https://sso.${INTERNAL_DOMAIN}`)
- **Category**: Apps without native OIDC/SAML support

See `docs/ssot/platform.auth.md` for full authentication strategy.

---

## 🔍 Outstanding Questions

### Question 1: OpenPanel SSO Support
**Status**: ✅ **Resolved** - OpenPanel does not support native OIDC or SAML.

**Solution**: Use Portal Gate (OAuth2-Proxy) for authentication.

**Next Action**:
- Deploy OpenPanel with local auth
- Configure Ingress with ForwardAuth to Portal Gate
- Test Casdoor → OAuth2-Proxy → OpenPanel login flow

### Question 2: Helm Chart Configuration Complexity
**Finding**: Helm chart requires substantial upfront configuration.

**Required Configuration**:
- External database connection strings
- Domain names and TLS certificates
- Secret generation (API keys, session keys)
- Resource limits and scaling parameters

**Approach**: Create detailed Terraform configuration following SigNoz pattern.

---

## ⏰ Timeline

- **2025-12-22**: L2 infrastructure completed (switched from PostHog to OpenPanel)
- **TBD**: OpenPanel SAML support verification
- **TBD**: L4 Helm deployment
- **TBD**: Authentication integration testing

---

*Last updated: 2025-12-22*
*Status: L2 infrastructure ready, awaiting SAML verification and L4 deployment*
