# Infra-022: TODOWRITE (Production Resilience & Disaster Recovery)

**Status**: Active  
**Owner**: Infra

## Purpose

跟踪 Infra-022 落地过程中发现的高优先级技术细节、配置卡点与待决事项。

## Top Issues

- [x] `bootstrap/01.dokploy_install/hostfw/`: 主机防火墙（T1.3 / #724 的"只开放 80/443/SSH"），owner 2026-09-17 批准。用 nftables 自有表而非 UFW（UFW 挡不住 Docker DNAT 的端口）。上线前公网可连 3000（Dokploy UI）、2377、7946、4789/udp；上线后外部只剩 22/80/443，runner→Dokploy、runner→主机 22、Cloudflare 路由、Dagster→OpenD 均正常；已 `install` 持久化（`infra2-hostfw.service` enabled）。未做：80/443 仅放行 Cloudflare IP 段（#724 §3）
- [x] `tools/host_backup.sh`（#618，truealpha#650 恢复演练发现）：每晚 prod 备份在 `platform/minio` 被 `tar` exit 1 + `set -e` 中止，finance_report/truealpha 的 pg dump 从未执行。改为 pg dump 先跑、minio 最后；`tar` exit 1 记 WARN；单个服务失败不再中止其余服务（`FAILED <id>`，残缺归档删除，不进 manifest，退出 1，跳过本地轮转）；redis `SAVE` 之前未鉴权（NOAUTH 且 exit 0，从未真正快照），现用容器自身 secrets 鉴权，只归档 `dump.rdb`。测试 `libs/tests/test_host_backup_script.py`。主机副本 2026-08-20 起已手工运行 PR 旧 head（sha256 `1e1285b9…`）；合入后需从 main 重装并核对 sha256（SOP-006）
- [ ] `tools/host_backup.sh` 现场覆盖验收：源代码已补齐 17/17 个 BackupFacet（原缺口另含 `platform/free`），并以双向 CI 检查防漂移；代码现把 prod/staging 的 manifest 与本地 `BACKUP_KEEP=7` 保留分别隔离，恢复演练默认只接受 Production manifest。待 VPS 安装同 SHA 脚本并分别证明两环境的 17/17 异地 manifest、字节校验与新增类别隔离恢复。
- [x] `tools/host_backup.sh`: 支持 Google Drive (rclone crypt E2EE) 分级异地备份（周备保留 60d，季度快照保留 730d），1Password 凭证已闭环，测试通过
- [x] `tools/run_restore_rehearsal.py`: 编写在独立临时沙箱容器中解密、导入并验证 5 项核心业务不变量的自动化演练工具，实测 10.62s 通过并用后即焚 0 残留
- [ ] `bootstrap/01.dokploy_install/host_guard/daemon.json`: daemon 默认日志限额、live-restore 与校验/回滚脚本已准备；待当前 head 的 owner 生产批准、VPS 应用、新建容器配置与业务在线验证。旧 Dokploy 控制面容器需后续安全重建才会继承默认值。
- [ ] `tools/disk_guardian.sh`: 80%/85% 守护与 systemd timer、模拟测试已准备；待 VPS 部署与 P1/P0 外部送达验收。
- [ ] `bootstrap/01.dokploy_install/host_guard/host-guard.env`: 独立主机、磁盘 P1、磁盘 P0 Healthchecks.io 检查及心跳 timer 已设计；真实 URL 只放 VPS root:root 0600 文件，待外部通知联动和停 ping 演练。
- [x] `tools/deploy_v2.py`: 接入 `pre_deploy_schema_check.py` fail-closed 门禁与 `ROLLBACK_CLASS` 计算。接入点是 `libs/deploy/promote.py:deploy()`（`deploy_v2()` staging/prod 分支唯一调用的 fixed-compose 后端），紧邻 `assert_approle_creds_present`/`preflight_vault_token` 之后、`client.update_compose`/`client.deploy_compose` 之前——与其它"任何变更前必须通过"门禁同一位置。按 TODOWRITE 约束在应用自己的已发布镜像内运行（`libs/deploy/schema_gate.py`：SSH 到 VPS，`docker exec` 从当前运行的 vault-agent sidecar 读取渲染好的 `DATABASE_URL`，再用即将部署的那个镜像 `--entrypoint python3 -` 跑检查脚本，`--network dokploy-network`）；实测 infra2 CI 进程与 iac-runner 都拿不到"docker daemon + 应用网络"两者兼备的环境（iac-runner 无 docker socket，`bootstrap/06.iac_runner/README.md` 的"只读 docker socket 挂载"与线上容器实测不符，已在 schema_gate.py 模块文档中记录），只有 VPS 主机本身两者都有。退出码 0 才放行，1（不一致）与 3（NOT EVALUATED）以及任何 SSH/docker 传输失败一律阻断。`ROLLBACK_CLASS`（A/C 两档，由 `tools/pre_deploy_schema_check.py::classify_rollback` 从同一次双向枚举比对推导，missing_in_code 或 casing 出现即 C）随检查结果一起打印（`ROLLBACK_CLASS: X`），deploy 侧解析并记录。只对 `ENUM_SOURCES` 已注册的服务生效（目前仅 `finance_report/app`）；`truealpha/app` 尚未注册，不受影响。`.github/workflows/deploy.yml` 的 `deploy` job 已加上 `INFRA2_WATCHDOG_SSH_*`（复用 ops-checks.yml 的同一组 secret，未新增）。**2026-09-22 实测：staging 与 prod 现在都跑不过这道门禁**（staging 4 处、prod 6 处历史遗留 enum 漂移，如 `chat_session_status_enum` 同时留有大小写两套标签）——本 PR 只负责把门禁接上，不负责清理漂移；这意味着合流后 finance_report/app 的下一次 staging/prod 部署会被真实阻断，需要先处理漂移或经 owner 决定的例外路径。
- [ ] `docs/runbooks/`: 撰写 Top 5 P0 告警对应的标准排障 Runbook
- [x] `libs/deploy/deployer.py`: #718 的「跳过前查容器」只做在线证明、不做 checkout 身份证明（否则每个 release 重启所有未变服务，#726），并修正其 compose 查询参数顺序（原实现查不到 compose，检查从未执行）
- [x] `libs/deploy/deployer.py` + `RestartAfterFacet`: redis 真正重部署（非跳过）并在线后，重启声明了 `restart_after: platform/redis` 的依赖方（OpenPanel api/worker、Authentik worker；staging 只有 Authentik worker），失败则 sync 失败并给出手工命令（#726，#713 同类）
- [x] `tools/infra_probe_runner.py`: 从未成功过的 round-trip 连续失败 ≥3 次且 ≥15min 后升级到 `InfraServiceProbeFailed`（声明的 severity）；只有自报配置缺失（`EX_CONFIG`）的失败永久留在 `InfraProbeMisconfigured`（#726）
- [ ] 手工 `docker restart platform-redis` / secret supply 触发的 redis 重启（`apply_secret_supply` 的 consumer restart）不经过依赖方重启路径；需要时在 redis 健康后再重启依赖方（#726 follow-up）
- [x] `openpanel-roundtrip` 与其 cascade root `openpanel-api-http` 声明为 `error`（P1，owner 2026-09-17 委托决定）；worker/dashboard 仍 `warning`，§5 改为 P2/P2（worker 停止落库由 round-trip 以 P1 发）；一次推送的 severity 取组内最高而非首个失败探针（`libs/infra_probes.group_severity`）；`signoz-roundtrip` 已是 `critical`=§5 的 P0，未变（#726）
- [x] `libs/backup_restore.py`: `CREATE ROLE postgres` 过滤只在 COPY 块外生效。原实现按行前缀过滤整个流，COPY 数据块内首列以该前缀开头的数据行会被静默丢弃——restore 仍然成形、`ON_ERROR_STOP` 不触发、演练照常 PASS，等于在"证明恢复可用"的工具里放过数据损坏。已实测 pg_dump 15 对 60 个长列名仍单行输出 `... FROM stdin;`，状态机对真实 dump 格式成立；过滤条数计入报告 `filtered_role_statements`（#754 audit）
- [x] `libs/backup_restore.py`: psql 带 `ON_ERROR_STOP=1`，可能在父进程仍在灌 482MB 时先退出。原实现让 `BrokenPipeError` 在 `proc.wait()` 之前逃逸，子进程不收尸，psql 真实退出码与 stderr（唯一的诊断依据）全部丢失。现兜住并仍取 `rc`（#754 audit）
- [x] `tools/run_restore_rehearsal.py`: `--service-id all` 循环按 #618 约定加 try/except——原实现无异常处理，finance_report 恒在列表首位，它一旦持续失败 truealpha 就永远不被演练，且 traceback 看起来像"一次演练失败"而非"一次演练从未发生"。另：`--database` 与 `all` 互斥（原会把同一个库名套给两个服务）；非 finance 服务的 `verified_accounts_count` 由 `0` 改为 `null`（`0` 是"查过且为零"的错误断言，与同报告 `domain_stats: {}` 自相矛盾；已确认全仓库无消费者）（#754 audit）
- [x] `tools/run_restore_rehearsal.py`: 不变量阈值重标定。truealpha 原 `≥1`/`≥100` 对实测 166/2830 是同义反复，丢 99.4% 数据仍 PASS；finance_report `accounts ≥5` 对实测正好 5 是零余量，删一个账号即误报。现取实测值约半数：TOPT `≥80`、GPPE `≥1400`、accounts `≥3`、finance 表 `≥40`（owner 2026-09-21 批准保守档）
- [ ] **VPS burn-in（owner 2026-09-21 决定：跑两个周期再说）**：新的 `--service-id all` 周期演练尚未独立跑满观察期。在新路径连续两个周期自行跑绿之前，不得退役旧路径。2026-09-23 只读核验：旧 `30 2 * * * /root/backups/truealpha-prod-backup.sh` cron 仍在，`/root/backups/truealpha-prod/` 有 9 月 19–23 日逐日产出的非空 `.dump`；它是同机并行兜底，不构成异地灾备。两周期后再决定是否摘除并在此打勾。注：truealpha 的 pg dump 也由 `tools/host_backup.sh` 产出；旧脚本是旁路而非唯一产出路径。
