# Infra-022: TODOWRITE (Production Resilience & Disaster Recovery)

**Status**: Active  
**Owner**: Infra

## Purpose

跟踪 Infra-022 落地过程中发现的高优先级技术细节、配置卡点与待决事项。

## Top Issues

- [ ] `tools/pg_backup_r2.sh`: 编写针对 Dokploy 托管 PostgreSQL 容器的定制导出与 rclone 同步脚本
- [ ] `tools/verify_backup_restore.sh`: 编写在独立临时容器中解密、导入并验证行数的验证脚本
- [ ] `bootstrap/docker_daemon.json`: 固化 `daemon.json` 并编写平滑热重载脚本（校验 live-restore 状态）
- [ ] `tools/disk_guardian.sh`: 编写磁盘双水位（80%/85%）守护脚本及 systemd timer
- [ ] `cloudflare/dead_man_switch`: 配置带外外部心跳检查与 Healthchecks.io / 飞书 Webhook 告警联动
- [ ] `tools/deploy_v2.py`: 接入 `pre_deploy_schema_check.py` fail-closed 门禁与 `ROLLBACK_CLASS` 计算
- [ ] `docs/runbooks/`: 撰写 Top 5 P0 告警对应的标准排障 Runbook
