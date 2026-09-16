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
- [ ] `tools/deploy_v2.py`: 接入 `pre_deploy_schema_check.py` fail-closed 门禁与 `ROLLBACK_CLASS` 计算（2026-09-16：门禁仍未被任何部署路径调用；接入时须在应用镜像/环境内运行——它 import 应用的 `src.database:Base.metadata`——并把退出码 1 与 3 都当作阻断）
- [ ] `docs/runbooks/`: 撰写 Top 5 P0 告警对应的标准排障 Runbook
- [x] `libs/deploy/deployer.py`: #718 的「跳过前查容器」只做在线证明、不做 checkout 身份证明（否则每个 release 重启所有未变服务，#726），并修正其 compose 查询参数顺序（原实现查不到 compose，检查从未执行）
