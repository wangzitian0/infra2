# Infra-008: TODOWRITE (Platform Multi-Env)

**Status**: Active  
**Owner**: Infra

## Purpose
Track top issues discovered during the project.

## Top Issues (Top 30)
- [x] `libs/dokploy.py`: environment selection missing; staging deploys target default env
- [x] `libs/deployer.py`: non-production requires explicit DATA_PATH or ENV_SUFFIX (guarded)
- [x] `bootstrap/06.iac_runner` and `finance/wealthfolio`: route ownership is single-source and uses Dokploy Domains for simple public HTTP routes; enforced by `libs/tests/test_domain_routing_policy.py`
- [ ] `platform/*/compose.yaml`: fixed `container_name` blocks multi-env (needs env suffix or remove container_name)
- [ ] `platform/*/compose.yaml`: Traefik labels use fixed router/service names (need appName/Dokploy domain API)
- [ ] `platform/*/deploy.py` and `platform/*/shared_tasks.py`: hardcoded container/domain references cause staging to hit production
- [ ] `platform/*`: env-specific values should move to Dokploy Environment (DATA_PATH / ENV_SUFFIX)
