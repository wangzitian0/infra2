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
- [ ] `tools/deploy_v2.py`: 接入 `pre_deploy_schema_check.py` fail-closed 门禁与 `ROLLBACK_CLASS` 计算。
      **owner 2026-09-22 已定：直接 fail-closed（退出码 1 与 3 都阻断）。** 物理约束见下方
      「Schema Gate 接入的物理约束」一节——naive 接法会阻断 100% 的部署，已实测。
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
- [ ] **VPS burn-in（owner 2026-09-21 决定：跑两个周期再说）**：PR #754 已在主机上退役旧的 unmanaged dump cron `/root/backups/truealpha-prod-backup.sh`，但新的 `--service-id all` 周期演练尚未独立跑满观察期。在新路径连续两个周期自行跑绿之前，不得认定旧路径可以退役；需在 VPS 上确认（或恢复）旧 cron 作为并行兜底，两周期后再正式摘除并在此打勾。注：消费链断裂的担忧已证伪——truealpha 的 pg dump 仍由 `tools/host_backup.sh:68` 产出，退役的只是旁路脚本

## Schema Gate 接入的物理约束（2026-09-22 实测，接之前必读）

`ops.standards.md` Rule 7 写着「必须执行」，工具写好、27 个测试全过，**被零个地方调用**。
owner 已定 fail-closed。但**直接在 `deploy_v2` 里调用会阻断 100% 的部署**——这是实测不是担心：

```
python tools/pre_deploy_schema_check.py --service finance_report/app     (infra2 环境) → exit 3
python tools/pre_deploy_schema_check.py --service truealpha/data_engine  (infra2 环境) → exit 3
NOT EVALUATED — deploy blocked ... No module named 'src' ... no enum source registered
```

> 它是仓库里的一个脚本，没有 console script / entry point——照着 `pre_deploy_schema_check`
> 敲在新 clone 里是跑不起来的。

### 为什么：门禁需要的两半住在不同地方

| 半边 | 只在哪拿得到 |
|---|---|
| **代码侧枚举**（服务自己的 SQLAlchemy metadata） | **只有应用镜像里有**（`src.orm_registry` + `src.database:Base.metadata`） |
| **数据库** | 只能从 VPS 的 Docker 网络连；而 `DATABASE_URL` **根本不在 compose 里**——vault-agent 在**运行时**把它渲染进正在跑的容器 |

而 `deploy_v2` 跑在 GitHub-hosted runner 上（`deploy.yml` 的 `deploy` job，`runs-on: ubuntu-latest`），
两半都没有。仓库自己的代码就写着这句——`libs/deploy/promote.py`：
*"the only view of the host this tier has (**it runs in GitHub Actions, no ssh**)"*。

### 已排除的路径

- **Dokploy API 没有 exec 能力**：`libs/dokploy.py` 只有 compose CRUD + `deploy_compose` /
  `redeploy_compose` + `get_containers` + 日志，没有「在容器里跑一条命令」。
- **App stack 不走 iac-runner**：`promote.deploy` 直连 Dokploy（`_deploy_fixed` 路由）。
  而改 iac-runner 属 L1 bootstrap，按 `AGENTS.md` 需 owner 批准 runner 重建。

### 可行路径（未验证——这正是 `tools/schema_gate_probe.py` 要去问的）

`deploy.yml` 的 `bootstrap` job **已经有 SSH 进 VPS 的能力**（`INFRA2_WATCHDOG_SSH_*`，
同一个 workflow 里已在用），`deploy` job 加同样三步即可。然后：

```bash
DB=$(docker exec <运行中的 backend> printenv DATABASE_URL)
docker run --rm -i --network dokploy-network -e DATABASE_URL="$DB" \
  --entrypoint python3 <新镜像> - --service finance_report/app  < tools/pre_deploy_schema_check.py
```

镜像用**新**的（代码侧枚举必须来自将要部署的那一版），DB URL 从**旧**容器读（数据库是同一个）。
脚本走 stdin 喂给 `python -`，免得把 infra2 的工具烤进应用镜像。

**插入点**：`libs/deploy/promote.py` 里 `client.deploy_compose(cfg.compose_id)` **之前**
（`update_compose_env` 之后、容器尚未重启时）。

### 接入前必须先问清的三个事实

1. 应用镜像的 WORKDIR 是什么（决定 `--app-path` 传什么，或是否根本不需要）
2. `src.orm_registry` + `src.database` 在镜像里 import 得了吗
3. `DATABASE_URL` 能不能从运行中的容器读回来

**照着猜写完、在一次 prod 部署里才发现猜错，顺序是反的。**
`tools/schema_gate_probe.py`（只读、dispatch-only）就是去问这三条的，第 4 项直接把门禁脚本
端到端真跑一次——它答得出来，接入就只剩编排。

### 适用性判据（owner 的选项里没覆盖，按 `AGENTS.md` 自行决定并说明依据）

「适用」= 服务在 `ENUM_SOURCES` 里登记（目前只有 `finance_report/app`）。
对**适用**的服务 fail-closed，退出码 1 与 3 都阻断。

未登记的持久化服务是**覆盖面**问题，不是 fail-closed 问题。
`libs/backup_verification.load_backup_inventory()` 列出了有持久化存储的服务，可作为
「应该被覆盖」的真源（数量随 `BackupFacet` 增删而变，所以这里不钉死一个数字——
要现值就跑它）——应该用一个**审计**让缺口可见，而不是用 fail-closed 把它伪装成「全挂」。
否则 owner 要的「防止 #698 复发」会变成「停掉所有部署」。

### 合流要求

接入 PR **触及 prod 部署路径**，按 `AGENTS.md` 需 owner 批准当时的 `head SHA`。
