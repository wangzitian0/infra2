# Infra-008: TODOWRITE (Platform Multi-Env)

**Status**: Active  
**Owner**: Infra

## Purpose
Track top issues discovered during the project.

## Top Issues (Top 30)
- [x] `libs/dokploy.py`: environment selection missing; staging deploys target default env
- [x] `libs/deployer.py`: non-production requires explicit DATA_PATH or ENV_SUFFIX (guarded)
- [ ] `platform/*/compose.yaml`: Traefik labels use fixed router/service names (need appName/Dokploy domain API)
- [ ] `platform/*`: env-specific values should move to Dokploy Environment (DATA_PATH / ENV_SUFFIX)
