# Infra-022: Production Resilience & Disaster Recovery (生产韧性与容灾兜底)

**Status**: In Progress  
**Owner**: Infra  
**Priority**: P0  
**Branch**: `feat/infra-022-resilience-and-dr`  
**Related Issues**: #798 (Epic 总览), #721 (备份与DR), #722 (回滚刹车), #723 (告警降噪), #724 (宿主机安全基线), #698 (Schema Gate)

## Goal

彻底消除单机 VPS 架构下的三大致命系统性风险（数据不可恢复、日志撑爆磁盘、坏发布无刹车与回滚撕裂数据），建立以“数据可恢复验证、宿主机防爆自愈、带外死人开关、发布三段式防御”为核心的生产韧性底座。

## Context

在反事实审计中，原规划的“4-Milestone 宏大蓝图”（涉及 Traefik 5% 动态加权分流、全量分布式追踪、自建控制平面）被证明脱离单机架构现实，且存在致命 P0 盲区：
1. **数据备份完全缺失（#721）**：系统无异地备份，RPO = ∞。一旦磁盘故障或人为误删，所有上层业务与可观测性归零。
2. **单机伪灰度与回滚炸弹（#722）**：单机 Traefik 动态加权灰度无法隔离内核与磁盘故障域；自动回滚在不可逆 DB migration 后会导致“老代码跑在新 Schema 上”的双向撕裂与数据损坏。
3. **宿主机防爆与带内失明（#724）**：Docker 日志无全局轮转上限易撑爆磁盘；SigNoz 与业务共因部署，VPS 崩溃时监控整体静默。
4. **告警噪音与狼来了（#723）**：新增多个告警源却缺乏分级、路由与 Runbook，陷入告警疲劳。

本项目作为**下一个唯一优先执行的生产里程碑**，全面废除过度设计，专注兜底。

## Scope

### L1: 宿主机防爆与安全底座 (Host & Engine)
- [ ] **T1.1 Docker 全局日志限额**：在 `/etc/docker/daemon.json` 配置 `max-size: 50m`, `max-file: 3`，配合 `live-restore: true` 实现平滑热重载。代码与校验脚本在 `bootstrap/01.dokploy_install/host_guard/`。2026-09-24 将 main `a6f29e5` 的配置和检查脚本临时送到 VPS，`configure_docker.sh --check` 返回 `configuration OK`，未改变 daemon；现场 `docker info` 仍显示 `live-restore=false`。待 owner 批准当前 head 后安装并验证新建容器。旧容器不会自动继承全局默认值。
- [ ] **T1.2 磁盘双水位自愈守护**：部署 `disk-guardian`（systemd timer）：≥80% 触发自动清理 dangling 镜像与构建缓存并发 P1 告警；≥85% 升级为 P0 告警并截断超大日志。代码与模拟测试已准备；待真实 timer 和外部通知验收。
- [ ] **T1.3 宿主机安全加固（#724）**：SSH 禁用密码认证、仅密钥登录；UFW 仅开放 80/443/SSH；Docker daemon 严禁暴露 TCP 端口。
  - 2026-09-17 进展：SSH 仅密钥 + fail2ban 已在主机生效（2026-09-15，手工）；公网只开放 80/443/SSH 已由 `bootstrap/01.dokploy_install/hostfw/`（nftables，替代 UFW）落地并持久化；Docker daemon 未监听 TCP 2375/2376。剩余：SSH 加固代码化、80/443 仅放行 Cloudflare 段。

### L2: 生产数据备份与带外容灾 (Platform & Data)
- [ ] **T2.1 全状态服务自动化异地备份至 Google Drive（#721）**：`rclone crypt` 与分级保留已打通；源代码的定时脚本现覆盖 17/17 个 `BackupFacet` 声明。2026-09-23 在 VPS 以 `tools/host_backup.sh` 文件 SHA256 `e2a897cf2d583e5b612946834c9d80b176339ebcc32c56c5e23da0df3c9cea83` 对 Staging 做独立本机 canary：17/17 项、总归档 282,318,058 字节，逐项 SHA256、字节数和 gzip/tar 可读性通过，目录 `0700`、manifest `0600`（`/data/backups/infra2-validation-staging-20260923T164904Z/staging-manifest.json`）。2026-09-24 将其中两个数据库归档分别还原至网络隔离、内存/CPU 限额的一次性 Postgres 容器：Finance Report 52 张表、5,327 条账户记录，TrueAlpha 144 张表、354 条完整抓取记录及 4,560 条可用结果；两次均退出 0、通过 3 项 SQL 不变量，容器已销毁。这证明新版**本机数据库归档**可恢复；其余 15 项未做功能恢复，也未证明异地上传/下载。现场异地最新可读清单（2026-09-18）仍只有 9/17 项，安装的宿主机脚本 SHA256 仍为 `b8f9b117a7bf426bb12c89cd307a7c89f91d25edf309c4852e1c4524404fc0a7`。仍须安装同 SHA 新脚本、取得生产和 Staging 各一轮 17/17 的异地 manifest 与字节校验，并完成新增归档的隔离恢复，才能称为全状态交付。
  - 2026-09-24 又用同一新源码在独立 root-only 目录 `/data/backups/infra2-validation-production-20260924T0200Z/` 运行 Production 本机 canary（`BACKUP_REMOTE` 显式 unset）：退出 0，17/17 项、2,726,743,453 字节；逐项重算 SHA256/大小，gzip/tar 读取全部通过，目录 `0700`、manifest `0600`。Finance Report 及 TrueAlpha 的生产归档分别在网络隔离的一次性容器中通过 5 项 SQL 不变量；还原库与当时在线库分别同为 52 张表/9 个账户、118 张表/178 条完整抓取/3,131 条可用结果。两容器均销毁。OpenPanel 和 ClickHouse 的在线 tar 有文件变化 WARN；ClickHouse 后续做了隔离查询验证，OpenPanel 尚未做功能恢复。**本次也未上传异地**。
  - 同一 Production canary 的两份 Redis RDB 在网络隔离的一次性实例中通过 `redis-check-rdb` 并启动：platform 还原 1,313 键，在线检查时 1,333 键（热数据变化）；finance_report 还原及在线均为 0 键。MinIO 归档在同版本隔离实例中启动并列出 6 个 bucket、11,306 个对象，抽取其中一个 405,214 字节对象成功读回并计算 SHA256。上述只证明选定状态可恢复；未验全部对象或异地包。临时容器与提取数据均已删除。
  - 同一 Production canary 的 ClickHouse 归档在同版本、网络隔离的一次性实例中启动，非系统表与在线同为 104 张；强制扫描后，SigNoz 日志还原 463,395 行（在线检查时 464,263），trace summary 还原 8,502,704 行（在线检查时 8,517,914）。这证明两个关键表可查询；在线写入持续发生，且未逐表验证一致性。临时容器及提取数据均已删除。
  - 同一 Production canary 的 platform Postgres 归档在网络隔离的一次性实例中还原，5 个数据库与在线实例名称、各库表数均一致：activepieces 64、authentik 314、openpanel 31、postgres 0、prefect 36。临时容器及其匿名数据卷已删除。
- [ ] **T2.2 备份自动恢复演练（Recovery Proof）**：沙箱工具和五项业务不变量已落地，历史手动运行在 10.62 秒内通过。但旧的默认 manifest 选择会取到周日较晚生成的 Staging 备份；代码现按环境分离并默认验证 Production manifest。仍须在 VPS 上用新脚本完成两次连续周周期的 Production `--service-id all` 演练，并保留期间的并行兜底。
- [ ] **T2.3 带外死人开关（Dead Man's Switch）**：宿主机 systemd timer 定时向外部 Healthchecks.io 上报心跳与磁盘 P1/P0 状态，Cloudflare Worker 的 30 分钟 cron 向第四个独立检查上报；主机失联与 Worker 停摆分别由外部通知。代码准备不等于现场验收，须配置四条真实检查 URL、生产安装并验证外部通知送达。
- [ ] **T2.4 整机故障恢复与 RTO 证明**：在隔离的新 VPS 上按 `ops.recovery` 的整机演练步骤重建信任根、控制面和数据，记录从故障宣告到公开服务及业务不变量恢复的耗时。现有 10.62 秒数据仅是单库沙箱还原耗时，不能作为整机 RTO。

### L3: 发布门禁闭环与告警降噪 (Deploy & Observability)
- [ ] **T3.1 发布三段式门禁与 Schema 防御（#698）**：
  - [ ] Stage 1: Ephemeral Smoke（构建后启动临时容器冒烟校验）
  - [x] Stage 2: Pre-flight Gate（`tools/pre_deploy_schema_check.py` 双向严格比对 + fail-closed；缺 DB URL / 代码侧枚举载入失败 = `NOT EVALUATED` 退出码 3 阻断，#718 review 修复；已接入 `deploy_v2`——`libs/deploy/promote.py:deploy()` 在任何 Dokploy 变更前调用 `libs/deploy/schema_gate.py`，SSH 到 VPS 用即将部署的应用镜像跑检查，退出码 1/3 及任何传输失败均阻断，详见 TODOWRITE 第 20 条）
  - [ ] Stage 3: Deploy + Synthetic Probes（部署后打真实业务探针，设 10 分钟观察烘焙期 T_Bake）
- [ ] **T3.2 安全回滚守则与人工刹车（#722）**：
  - [x] 明确 `ROLLBACK_CLASS`：仅 Class A（无破坏性 DDL）允许自动回滚；出现 DROP/RENAME/收紧约束（Class C）**严禁自动回滚**，必须人工挂起并 forward-fix。`tools/pre_deploy_schema_check.py::classify_rollback` 已实现并随 Stage 2 门禁的每次比对一起计算、打印（`ROLLBACK_CLASS: A|C`）——门禁本身只有两档可达（casing 漂移必然同时触发 missing_in_code，不存在可达的中间档）。**这只是分类，不是执行器**：下面两项（自动回滚熔断、`--force-promote`）仍未实现，本仓库目前没有任何自动回滚路径可供这个分类去约束。
  - [ ] 自动回滚上限熔断：最多自动回滚 1 次，严禁运行 `migrate down`。
  - [ ] 支持 `--force-promote` 强行放行参数（必须携带 `--reason` 与 `--operator` 并留痕）。
- [ ] **T3.3 告警信噪比治理与 P0 Runbook（#723）**：
  - 告警严格分级（P0 立即叫人 / P1 日间处理），同源 5 分钟去重聚合。
  - Top 5 P0 告警必须提供 3 步内可执行的排障 Runbook 链接。

## Deliverables

1. `/etc/docker/daemon.json` IaC 配置与平滑应用脚本。
2. `tools/disk_guardian.sh` 与 systemd timer 磁盘自愈组件。
3. `tools/host_backup.sh` 异地加密备份脚本（Google Drive / rclone crypt）+ `tools/run_restore_rehearsal.py` 沙箱恢复演练（VPS crontab 周期执行）。
4. 带外死人开关配置与 Cloudflare Worker 探针集成。
5. `tools/pre_deploy_schema_check.py` 双向比对与回滚安全门禁（已加固）。
6. P0 告警 Runbook 文档集 (`docs/runbooks/`).

## PR Links

- #798 (Epic 交付闭环总览), #721, #722, #723, #724 (Tracking Issues)
- #618 host_backup: 单服务失败不中止整轮（T2.1 前置）
- 待关联后续实现 PR

## Change Log

| Date | Change |
|---|---|
| 2026-09-24 | VPS 只读核验：`infra2-disk-guardian.timer` 和 `infra2-host-heartbeat.timer` 均不存在，`/etc/infra2/host-guard.env` 及安装脚本均不存在；Docker `live-restore=false`，磁盘使用 70%。用 main `a6f29e5` 的脚本运行 `configure_docker.sh --check`，daemon 候选配置校验通过，未应用。T1.1/T1.2/T2.3 仍未完成。 |
| 2026-09-24 | Production platform Postgres 归档在隔离实例中还原，5 个数据库及各库表数与在线一致；匿名数据卷已删除。 |
| 2026-09-24 | Production ClickHouse 归档在隔离实例中启动，104 张非系统表可见，SigNoz 日志与 trace summary 实际扫描查询成功；仍待其他表、异地包与整机恢复验证。 |
| 2026-09-24 | Production canary 的 Redis RDB 与 MinIO 归档通过隔离实例实际加载；MinIO 列出 11,306 个对象并读回一个对象，临时数据清理。异地恢复仍待验证。 |
| 2026-09-24 | 生产 TrueAlpha 首次隔离恢复撞到官方 Postgres 镜像临时服务器的就绪竞态；恢复工具改为等初始化完成标记和最终服务器就绪，重跑 5 项不变量通过。独立复审又发现 `docker rm -f` 会留下含恢复数据的匿名卷；本次演练留下的 5 个卷按创建时间、Postgres 内容和 dangling 状态核对后逐个删除，恢复工具改为清理容器及匿名卷，并隔离每次下载目录。 |
| 2026-09-24 | Production 新脚本本机 canary 17/17，17 个文件哈希与读取均通过；Finance Report 和 TrueAlpha 从该归档恢复后通过 5 项不变量且行数与在线库相同。仍待异地 17/17。 |
| 2026-09-24 | Staging 新归档中 Finance Report 和 TrueAlpha 的 Postgres 数据在 VPS 一次性网络隔离容器中实际恢复并通过 SQL 检查；两个容器均清理。异地最新清单仍只有 9/17，T2.1 保持未完成。 |
| 2026-09-23 | Staging 本机 17/17 备份 canary 从新源码直接运行，独立 root-only 目录，17 个归档均通过 SHA256、大小与 gzip/tar 读取检查；未上传异地，未做功能恢复，T2.1 保持未完成。 |
| 2026-09-23 | 宿主机现场核查：93 个运行容器中 89 个已有容器级日志限额，4 个未设限的是 Dokploy 控制面；daemon 无默认日志限额及 live-restore，已有每 6 小时运行的 host hygiene。新增 daemon 配置校验/回滚脚本、5 分钟磁盘守护、1 分钟带外心跳及三条独立外部检查的安装方案；生产验收前 T1.1/T1.2/T2.3 保持未完成。 |
| 2026-09-22 | Stage 2 接入 `deploy_v2`：`libs/deploy/promote.py:deploy()` 在任何 Dokploy 变更前调用新增的 `libs/deploy/schema_gate.py`（SSH 到 VPS，用即将部署的应用镜像跑检查——infra2 CI 与 iac-runner 都没有"docker daemon + 应用网络"兼备的环境，只有 VPS 主机有），exit 0 才放行，exit 1/3 及任何传输失败一律阻断。同时给 `tools/pre_deploy_schema_check.py` 加上 `classify_rollback`（A/C 两档，casing 漂移必然伴随 missing_in_code，不存在可达的中间档），随门禁结果一起打印 `ROLLBACK_CLASS`。仅对 `ENUM_SOURCES` 已注册服务生效（目前只有 `finance_report/app`）。实测：staging/prod 当前各有历史遗留 enum 漂移，本次接入后会真实阻断下一次 finance_report/app 部署，需先处理或走例外路径 |
| 2026-09-21 | 完成 T2.2：编写并实测 `tools/run_restore_rehearsal.py`，沙箱临时容器拉取 Google Drive 加密归档完成灌库与 5 项不变量校验，用后即焚 0 污染，实测 10.62s 通过 |
| 2026-09-18 | 完成 T2.1：基于 rclone crypt 落地 Google Drive 异地端到端加密备份，实现周备(60d)+季度快照(2年)分级保留，实测打通读写验证 |
| 2026-09-16 | 基于反事实审计全面重构路线图，正式立项 Infra-022，废除过度工程规划，确立生产韧性与 DR 为下一里程碑 |
| 2026-09-17 | T2.1 前置：`tools/host_backup.sh` 不再因单个服务失败中止整轮（#618，truealpha#650），pg dump 先跑，redis SAVE 鉴权；SSOT SOP-006 记录失败契约与覆盖缺口 |
| 2026-09-16 | Schema Gate：#718 review 两条（枚举模块路径不存在、无 DB URL 时返回成功）修复为 `NOT EVALUATED`（退出码 3）阻断；代码侧改读服务自己的 SQLAlchemy metadata（`ENUM_SOURCES`） |

## Verification

| # | Check | Target Invariant | Verification Command |
|---|---|---|---|
| 1 | Docker 日志限额 | 所有容器日志受限 ≤ 150MB | `docker inspect -f '{{json .HostConfig.LogConfig.Config}}' <c>` 包含 `max-size: 50m` |
| 2 | 磁盘自愈机制 | 模拟使用率超标触发清理与通知 | `fallocate` 触发 `disk_guardian.sh`，确认 dangling 缓存清除且告警送达 |
| 3 | 异地备份就绪 | Google Drive 存在加密备份包且 SHA256 吻合 | `rclone lsd gdrive-backup:infra2/` 验证目录存在且可读写 |
| 4 | 恢复演练闭环 | 自动化还原 Production 异地备份到临时库并通过 SQL 抽样 | `tools/run_restore_rehearsal.py --service-id all` 在新环境标记与最新指针上连续两个周周期跑通，每个服务各输出 `RESTORE_PROOF: PASS`，退出码 0；旧路径曾单次 10.62s 通过，不能代替此项 |
| 5 | 死人开关兜底 | 宿主机断网 10 分钟外部独立告警 | 停止心跳上报，Healthchecks.io 外部通道（飞书/邮件）在 10 分钟内报警 |
| 6 | Schema Gate 门禁 | 数据库与代码 Enum/Schema 不一致即阻断；缺输入（无 DB URL / 枚举载入失败）同样阻断；且真的接在 `deploy_v2` 的部署路径上，不只是模块自证 | `pytest libs/tests/test_pre_deploy_schema_check.py libs/tests/test_schema_gate.py libs/tests/test_deploy_primitive.py -k schema_gate` 全绿；实机验证见 TODOWRITE 第 20 条 |
| 7 | ROLLBACK_CLASS 分类 | 破坏性信号（DROP/RENAME 已落库的枚举，即 missing_in_code）分类为 C，纯新增分类为 A | `pytest libs/tests/test_pre_deploy_schema_check.py -k classify_rollback` 全绿；门禁每次比对都打印 `ROLLBACK_CLASS: A|C`。**尚无**自动回滚执行器/熔断/`--force-promote`（T3.2 其余两项未实现）——本行只验证分类，不验证回滚回路。

## References

- [Issue #721: 数据备份与灾难恢复](https://github.com/wangzitian0/infra2/issues/721)
- [Issue #722: 自动回滚人工刹车](https://github.com/wangzitian0/infra2/issues/722)
- [Issue #723: 告警分级与信噪比治理](https://github.com/wangzitian0/infra2/issues/723)
- [Issue #724: 安全基线与死人开关](https://github.com/wangzitian0/infra2/issues/724)
- [SSOT: ops.standards.md](../ssot/ops.standards.md)
- [SSOT: ops.observability.md](../ssot/ops.observability.md)
