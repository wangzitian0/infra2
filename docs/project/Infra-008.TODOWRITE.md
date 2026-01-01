# Infra-008: TODOWRITE (Platform Multi-Env)

**Status**: Active  
**Owner**: Infra

## Purpose
Track top issues discovered during the project.

## Top Issues (Top 30)
- [x] `libs/dokploy.py`: environment selection missing; staging deploys target default env
- [x] `platform/*/compose.yaml`: container_name and volume paths are production-only (no env isolation)
- [x] `platform/*/compose.yaml`: domain labels use fixed subdomains (no env suffix)
- [x] `platform/*/deploy.py`: data_path and domain output not env-aware
- [x] `platform/*/shared_tasks.py`: hardcoded container/domain references
