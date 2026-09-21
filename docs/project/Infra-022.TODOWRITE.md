# Infra-022: TODOWRITE (Production Resilience & Disaster Recovery)

**Status**: Active  
**Owner**: Infra

## Purpose

跟踪 Infra-022 落地过程中发现的高优先级技术细节、配置卡点与待决事项。

## Top Issues

- [x] `bootstrap/01.dokploy_install/hostfw/`: 主机防火墙（T1.3 / #724 的"只开放 80/443/SSH"），owner 2026-09-17 批准。用 nftables 自有表而非 UFW（UFW 挡不住 Docker DNAT 的端口）。上线前公网可连 3000（Dokploy UI）、2377、7946、4789/udp；上线后外部只剩 22/80/443，runner→Dokploy、runner→主机 22、Cloudflare 路由、Dagster→OpenD 均正常；已 `install` 持久化（`infra2-hostfw.service` enabled）。未做：80/443 仅放行 Cloudflare IP 段（#724 §3）
- [x] `tools/host_backup.sh`（#618，truealpha#650 恢复演练发现）：每晚 prod 备份在 `platform/minio` 被 `tar` exit 1 + `set -e` 中止，finance_report/truealpha 的 pg dump 从未执行。改为 pg dump 先跑、minio 最后；`tar` exit 1 记 WARN；单个服务失败不再中止其余服务（`FAILED <id>`，残缺归档删除，不进 manifest，退出 1，跳过本地轮转）；redis `SAVE` 之前未鉴权（NOAUTH 且 exit 0，从未真正快照），现用容器自身 secrets 鉴权，只归档 `dump.rdb`。测试 `libs/tests/test_host_backup_script.py`。主机副本 2026-08-20 起已手工运行 PR 旧 head（sha256 `1e1285b9…`）；合入后需从 main 重装并核对 sha256（SOP-006）
- [ ] `tools/host_backup.sh` 覆盖缺口：BackupFacet 清单中 `bootstrap/1password`、`bootstrap/iac_runner`、`platform/alerting`、`platform/openpanel`、`platform/portal`、`platform/signoz`、`truealpha/data_engine` 未被脚本归档；prod 与 staging 共用 `/data/backups/infra2`，`BACKUP_KEEP=7` 约等于各 3.5 天
- [x] `tools/host_backup.sh`: 支持 Google Drive (rclone crypt E2EE) 分级异地备份（周备保留 60d，季度快照保留 730d），1Password 凭证已闭环，测试通过
- [x] `tools/run_restore_rehearsal.py`: 编写在独立临时沙箱容器中解密、导入并验证 5 项核心业务不变量的自动化演练工具，实测 10.62s 通过并用后即焚 0 残留
- [ ] `bootstrap/docker_daemon.json`: 固化 `daemon.json` 并编写平滑热重载脚本（校验 live-restore 状态）
- [ ] `tools/disk_guardian.sh`: 编写磁盘双水位（80%/85%）守护脚本及 systemd timer
- [ ] `cloudflare/dead_man_switch`: 配置带外外部心跳检查与 Healthchecks.io / 飞书 Webhook 告警联动
- [ ] `tools/deploy_v2.py`: 接入 `pre_deploy_schema_check.py` fail-closed 门禁与 `ROLLBACK_CLASS` 计算（2026-09-16：门禁仍未被任何部署路径调用；接入时须在应用镜像/环境内运行——它 import 应用的 `src.database:Base.metadata`——并把退出码 1 与 3 都当作阻断）
- [ ] `docs/runbooks/`: 撰写 Top 5 P0 告警对应的标准排障 Runbook
- [x] `libs/deploy/deployer.py`: #718 的「跳过前查容器」只做在线证明、不做 checkout 身份证明（否则每个 release 重启所有未变服务，#726），并修正其 compose 查询参数顺序（原实现查不到 compose，检查从未执行）
- [x] `libs/deploy/deployer.py` + `RestartAfterFacet`: redis 真正重部署（非跳过）并在线后，重启声明了 `restart_after: platform/redis` 的依赖方（OpenPanel api/worker、Authentik worker；staging 只有 Authentik worker），失败则 sync 失败并给出手工命令（#726，#713 同类）
- [x] `tools/infra_probe_runner.py`: 从未成功过的 round-trip 连续失败 ≥3 次且 ≥15min 后升级到 `InfraServiceProbeFailed`（声明的 severity）；只有自报配置缺失（`EX_CONFIG`）的失败永久留在 `InfraProbeMisconfigured`（#726）
- [ ] 手工 `docker restart platform-redis` / secret supply 触发的 redis 重启（`apply_secret_supply` 的 consumer restart）不经过依赖方重启路径；需要时在 redis 健康后再重启依赖方（#726 follow-up）
- [x] `openpanel-roundtrip` 与其 cascade root `openpanel-api-http` 声明为 `error`（P1，owner 2026-09-17 委托决定）；worker/dashboard 仍 `warning`，§5 改为 P2/P2（worker 停止落库由 round-trip 以 P1 发）；一次推送的 severity 取组内最高而非首个失败探针（`libs/infra_probes.group_severity`）；`signoz-roundtrip` 已是 `critical`=§5 的 P0，未变（#726）
