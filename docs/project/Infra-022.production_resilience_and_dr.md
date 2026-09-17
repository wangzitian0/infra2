# Infra-022: Production Resilience & Disaster Recovery (生产韧性与容灾兜底)

**Status**: In Progress  
**Owner**: Infra  
**Priority**: P0  
**Branch**: `feat/infra-022-resilience-and-dr`  
**Related Issues**: #721 (备份与DR), #722 (回滚刹车), #723 (告警降噪), #724 (宿主机安全基线), #698 (Schema Gate)

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
- [ ] **T1.1 Docker 全局日志限额**：在 `/etc/docker/daemon.json` 配置 `max-size: 50m`, `max-file: 3`，配合 `live-restore: true` 实现平滑热重载。
- [ ] **T1.2 磁盘双水位自愈守护**：部署 `disk-guardian`（systemd timer）：≥80% 触发自动清理 dangling 镜像与构建缓存并告警；≥85% 升级为 P0 告警并激进截断日志。
- [ ] **T1.3 宿主机安全加固（#724）**：SSH 禁用密码认证、仅密钥登录；UFW 仅开放 80/443/SSH；Docker daemon 严禁暴露 TCP 端口。
  - 2026-09-17 进展：SSH 仅密钥 + fail2ban 已在主机生效（2026-09-15，手工）；公网只开放 80/443/SSH 已由 `bootstrap/01.dokploy_install/hostfw/`（nftables，替代 UFW）落地并持久化；Docker daemon 未监听 TCP 2375/2376。剩余：SSH 加固代码化、80/443 仅放行 Cloudflare 段。

### L2: 生产数据备份与带外容灾 (Platform & Data)
- [ ] **T2.1 PostgreSQL 自动化异地备份至 Cloudflare R2（#721）**：每日定时执行 `pg_dump -Fc`（结构化压缩），使用 `age` 客户端加密后通过 rclone 上传至 Cloudflare R2（零出网费），保留 30 天。
- [ ] **T2.2 备份自动恢复演练（Recovery Proof）**：每周通过自动化脚本在临时容器拉取最新备份执行 `pg_restore` 真实恢复并抽验关键业务行数，“未验证过的备份不等于备份”。
- [ ] **T2.3 带外死人开关（Dead Man's Switch）**：宿主机定时向外部 Healthchecks.io 上报心跳，结合 Cloudflare Worker 带外探针；一旦宿主机系统死锁或网络中断，外部独立通道立即触发 P0 告警。

### L3: 发布门禁闭环与告警降噪 (Deploy & Observability)
- [ ] **T3.1 发布三段式门禁与 Schema 防御（#698）**：
  - [ ] Stage 1: Ephemeral Smoke（构建后启动临时容器冒烟校验）
  - [x] Stage 2: Pre-flight Gate（`tools/pre_deploy_schema_check.py` 双向严格比对 + fail-closed；缺 DB URL / 代码侧枚举载入失败 = `NOT EVALUATED` 退出码 3 阻断，#718 review 修复；尚未接入 `deploy_v2`）
  - [ ] Stage 3: Deploy + Synthetic Probes（部署后打真实业务探针，设 10 分钟观察烘焙期 T_Bake）
- [ ] **T3.2 安全回滚守则与人工刹车（#722）**：
  - 明确 `ROLLBACK_CLASS`：仅 Class A（无破坏性 DDL）允许自动回滚；出现 DROP/RENAME/收紧约束（Class C）**严禁自动回滚**，必须人工挂起并 forward-fix。
  - 自动回滚上限熔断：最多自动回滚 1 次，严禁运行 `migrate down`。
  - 支持 `--force-promote` 强行放行参数（必须携带 `--reason` 与 `--operator` 并留痕）。
- [ ] **T3.3 告警信噪比治理与 P0 Runbook（#723）**：
  - 告警严格分级（P0 立即叫人 / P1 日间处理），同源 5 分钟去重聚合。
  - Top 5 P0 告警必须提供 3 步内可执行的排障 Runbook 链接。

## Deliverables

1. `/etc/docker/daemon.json` IaC 配置与平滑应用脚本。
2. `tools/disk_guardian.sh` 与 systemd timer 磁盘自愈组件。
3. `tools/pg_backup_r2.sh` 备份脚本 + GitHub Actions `restore-verification.yml` 自动恢复演练工作流。
4. 带外死人开关配置与 Cloudflare Worker 探针集成。
5. `tools/pre_deploy_schema_check.py` 双向比对与回滚安全门禁（已加固）。
6. P0 告警 Runbook 文档集 (`docs/runbooks/`).

## PR Links

- #721, #722, #723, #724 (Tracking Issues)
- #618 host_backup: 单服务失败不中止整轮（T2.1 前置）
- 待关联后续实现 PR

## Change Log

| Date | Change |
|---|---|
| 2026-09-16 | 基于反事实审计全面重构路线图，正式立项 Infra-022，废除过度工程规划，确立生产韧性与 DR 为下一里程碑 |
| 2026-09-17 | T2.1 前置：`tools/host_backup.sh` 不再因单个服务失败中止整轮（#618，truealpha#650），pg dump 先跑，redis SAVE 鉴权；SSOT SOP-006 记录失败契约与覆盖缺口 |
| 2026-09-16 | Schema Gate：#718 review 两条（枚举模块路径不存在、无 DB URL 时返回成功）修复为 `NOT EVALUATED`（退出码 3）阻断；代码侧改读服务自己的 SQLAlchemy metadata（`ENUM_SOURCES`） |

## Verification

| # | 验证项 | 验收标准 (DoD) | 验证命令 / Proof |
|---|---|---|---|
| 1 | Docker 日志限额 | 所有容器日志受限 ≤ 150MB | `docker inspect -f '{{json .HostConfig.LogConfig.Config}}' <c>` 包含 `max-size: 50m` |
| 2 | 磁盘自愈机制 | 模拟使用率超标触发清理与通知 | `fallocate` 触发 `disk_guardian.sh`，确认 dangling 缓存清除且告警送达 |
| 3 | 异地备份就绪 | R2 存在加密备份包且 SHA256 吻合 | `rclone ls r2:pg-backups/` 验证最新备份文件存在 |
| 4 | 恢复演练闭环 | 自动化还原到临时库并通过 SQL 抽样 | `bash tools/verify_backup_restore.sh` 跑通并输出 `RESTORE_PROOF: PASS` |
| 5 | 死人开关兜底 | 宿主机断网 10 分钟外部独立告警 | 停止心跳上报，Healthchecks.io 外部通道（飞书/邮件）在 10 分钟内报警 |
| 6 | Schema Gate 门禁 | 数据库与代码 Enum/Schema 不一致即阻断；缺输入（无 DB URL / 枚举载入失败）同样阻断 | `pytest libs/tests/test_pre_deploy_schema_check.py` 全绿 |
| 7 | 回滚熔断与刹车 | 破坏性 migration 场景下阻止自动回滚 | 门禁输出 `ROLLBACK_CLASS: C` 并阻断自动回滚回路 |

## References

- [Issue #721: 数据备份与灾难恢复](https://github.com/wangzitian0/infra2/issues/721)
- [Issue #722: 自动回滚人工刹车](https://github.com/wangzitian0/infra2/issues/722)
- [Issue #723: 告警分级与信噪比治理](https://github.com/wangzitian0/infra2/issues/723)
- [Issue #724: 安全基线与死人开关](https://github.com/wangzitian0/infra2/issues/724)
- [SSOT: ops.standards.md](../ssot/ops.standards.md)
- [SSOT: ops.observability.md](../ssot/ops.observability.md)
