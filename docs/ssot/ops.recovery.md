# 故障恢复 SSOT

> **SSOT Key**: `ops.recovery`
> **核心定义**: 定义故障恢复策略、紧急绕过路径 (Break-glass) 及数据还原流程。

---

## 1. 真理来源 (The Source)

> **原则**：1Password 是最终的信任根 (Root of Trust)。只要它还在，基础设施就可重建。

| 维度 | 物理位置 (SSOT) | 说明 |
|------|----------------|------|
| **Master Keys** | **1Password** | Root Token, Unseal Keys, SSH Keys |
| **数据备份** | `/data` + 各服务 `deploy.py` 的 `BackupFacet` 声明（派生：[`libs/backup/`](../../libs/backup/README.md) [`libs/backup/verification.py::load_backup_inventory`]，#542）+ off-host manifest | DB dumps and persistent data archives |
| **代码仓库** | **GitHub** | 部署代码、Compose 定义 |

---

## 2. 架构模型 (恢复路径)

```mermaid
graph TD
    Failure((故障发生))

    subgraph "L1 Recovery"
        Failure -->|Dokploy 挂了| Reinstall[重新安装 Dokploy]
        Reinstall -->|依赖| OP_SSH[1Password: SSH Key]
    end

    subgraph "L2 Recovery"
        Failure -->|Vault Sealed| Unseal[Vault Unseal]
        Unseal -->|依赖| OP_KEYS[1Password: Unseal Keys]

        Failure -->|SSO 不可用| RootLogin[Vault Root Token]
        RootLogin -->|依赖| OP_ROOT[1Password: Root Token]
    end

    subgraph "L3 Data Recovery"
        Failure -->|DB 丢数据| Restore[PG Restore]
        Restore -->|依赖| BACKUP[备份文件]
    end
```

---

## 3. 设计约束 (Dos & Don'ts)

### ✅ 推荐模式 (Whitelist)

- **模式 A**: 必须定期验证 1Password 中的密钥是否有效（演练）。
- **模式 B**: 必须将 `/data` 的关键数据备份到异地。
- **模式 C**: 每个 deployer-owned `DATA_PATH` 必须在 backup inventory 中登记 owner、method、RPO、retention 和 restore command。

### ⛔ 禁止模式 (Blacklist)

- **反模式 A**: **禁止** 仅依赖 Vault 存储自身的 Unseal Keys（死锁）。
- **反模式 B**: **禁止** 无备份直接清理 `/data`。

---

## 4. 标准操作程序 (Playbooks)

### SOP-001: Vault 解封 (Unseal)

- **触发条件**: Vault 重启后处于 Sealed 状态
- **步骤**:
    1. 获取 Keys: `op item get "bootstrap/vault/Unseal Keys" --vault "Infra2" --reveal`
    2. 进入 VPS 并执行：
       ```bash
       ssh root@<VPS_HOST>
       export VAULT_ADDR=https://vault.<INTERNAL_DOMAIN>
       vault operator unseal <key1>
       vault operator unseal <key2>
       vault operator unseal <key3>
       ```

### SOP-002: 平台数据库恢复

- **触发条件**: Platform PG 数据损坏
- **步骤**:
    1. 停止相关服务（Dokploy Stop）。
    2. 恢复数据：
       ```bash
       ssh root@<VPS_HOST>
       docker exec -i platform-postgres${ENV_SUFFIX} psql -U postgres < /data/backups/latest.sql
       ```
    3. 启动服务并验证健康。

### SOP-003: 紧急访问 (Break-glass)

- **触发条件**: SSO 不可用，需操作 Vault
- **步骤**:
    1. 获取 Root Token: `op item get "bootstrap/vault/Unseal Keys" --vault "Infra2" --reveal`
    2. 登录: `vault login <root_token>`

### SOP-004: 备份 freshness 验证

备份系统必须产出一个 off-host manifest，至少包含：

```json
{
  "artifacts": [
    {
      "service_id": "platform/postgres",
      "created_at": "2026-06-05T00:00:00Z",
      "size_bytes": 123456,
      "sha256": "<64 hex chars>",
      "remote_uri": "gdrive-backup:infra2/weekly/20260920T033000Z/platform/postgres/archive.tar.gz"
    }
  ]
}
```

验证命令：

```bash
uv run python tools/backup_verification.py --manifest /path/to/manifest.json --json
```

失败条件包括：manifest 缺少服务、artifact 超过 RPO、size 为空、checksum 缺失、
或 `remote_uri` 不是 inventory 指定的 off-host remote。

### SOP-005: 生成并上传 off-host 备份

备份 runner 读取各服务 `BackupFacet` 声明派生的清单（[`libs/backup/`](../../libs/backup/README.md) `libs/backup/verification.py::load_backup_inventory` / 兼容门面 `libs/backup_verification.py`，#542），
为每个登记的 `data_path` 创建 archive、计算 SHA256，并通过主机上的 `rclone`
remote 上传到 off-host storage。

这个按声明生成文件归档的命令目前是手工入口；宿主机定时任务使用下面的
`host_backup.sh`。不能用这里的示例命令证明每周的异地备份已经运行。

Dry-run 不上传：

```bash
uv run python tools/backup_runner.py --output-dir /tmp/infra2-backups --no-upload
```

生产上传示例：

```bash
BACKUP_REMOTE=gdrive-backup:infra2 uv run python tools/backup_runner.py \
  --output-dir /data/backups/infra2 \
  --manifest /data/backups/infra2/manifest.json
```

`rclone` remote credentials live in 1Password `bootstrap/gdrive` (`rclone_conf` field).
They must not be committed to this repository.

### SOP-006: On-host scheduled backup runner (logical dumps + tiered retention)

`tools/host_backup.sh` is the on-host scheduled backup runner. Unlike the
inventory archiver, it produces **restorable logical backups**:

- Postgres services: `pg_dumpall` via `docker exec` (crash-consistent). They run
  **first**.
- Redis services: an authenticated `redis-cli SAVE` (`REDISCLI_AUTH` from the
  container's `/secrets/.env`), then archive `dump.rdb` only. A SAVE that does not
  answer `OK` logs `WARN` and the archive holds the last automatic snapshot
  (`--save 60 1`).
- Other data paths: gzip tar, **last** (minio is the busiest live tree).

Failure contract (#618, found by the truealpha#650 restore drill):

- `tar` exit 1 (a live file changed or vanished mid-read) is a `WARN` and the
  archive is kept as crash-consistent. Exit ≥2 fails that service.
- A failing service never stops the others. It is logged as `FAILED <service_id>`,
  its partial archive is removed, it is absent from the manifest (so SOP-004
  reports it missing), and the run exits 1.
- Before #618 the first failure ended the run under `set -e`: every scheduled
  prod run stopped at `platform/minio` and never dumped the finance_report or
  truealpha databases.

It writes a `tools/backup_verification.py`-compatible manifest and, when
`BACKUP_REMOTE` (an rclone target) is set, uploads each archive off-host into
the tier/environment directory (`${REMOTE}/${BACKUP_TIER}/${ENVIRONMENT}/${TS}`). Local retention keeps the
most recent `BACKUP_KEEP` (default 7) run directories **per environment** and is
skipped on a failed run. Run directories are named `production-<timestamp>` or
`staging-<timestamp>` under `/data/backups/infra2`; each run's manifest records
its environment. The latest pointers are `production-manifest.json` and
`staging-manifest.json` in that directory. The off-host latest manifests are
likewise separate (`gdrive-backup:infra2/production/manifest.json` and
`gdrive-backup:infra2/staging/manifest.json`). Legacy unprefixed local runs are
left untouched for an operator to retire after the new path is verified.
Because the set includes Vault and 1Password Connect state, the runner uses
`umask 077` and makes `/data/backups/infra2` root-only (`0700`). On rollout,
check this on the host before the next run; restricting this parent also shields
legacy archives that were created with looser modes.
Remote retention automatically prunes expired snapshots (`weekly` > 60d; `quarterly` > 730d).

The script's service list must equal the `BackupFacet` inventory. The list is
currently hand-kept because the installed host copy runs without a repo Python
environment; `libs/tests/test_host_backup_script.py` checks both directions and
the declared data paths. A new facet without a scheduled backup entry fails CI.
Bootstrap paths are shared by both environments; environment-scoped paths take
`ENV_SUFFIX`. Source coverage alone is not runtime proof: after merging a script
change, reinstall the host copy, verify its checksum, and inspect a fresh
off-host manifest and restored artifact for every required entry.

The host copy is installed by hand from `main` (script-deploy drift belongs to
the reconcile lane). After a change merges, reinstall it and compare checksums:

```bash
install -m 0755 tools/host_backup.sh /usr/local/sbin/infra2-host-backup.sh
sha256sum tools/host_backup.sh /usr/local/sbin/infra2-host-backup.sh
```

Scheduled on the host via crontab:

```cron
# Weekly backups (Sundays 03:30 UTC, rolling 60d window)
30 3 * * 0 BACKUP_REMOTE=gdrive-backup:infra2 BACKUP_TIER=weekly /usr/local/sbin/infra2-host-backup.sh >> /var/log/infra2-backup.log 2>&1
45 3 * * 0 BACKUP_REMOTE=gdrive-backup:infra2 BACKUP_TIER=weekly ENV_SUFFIX=-staging /usr/local/sbin/infra2-host-backup.sh >> /var/log/infra2-backup-staging.log 2>&1

# Quarterly long-term snapshots (04:00 UTC on Jan 1, Apr 1, Jul 1, Oct 1, 2-year retention)
0 4 1 1,4,7,10 * BACKUP_REMOTE=gdrive-backup:infra2 BACKUP_TIER=quarterly /usr/local/sbin/infra2-host-backup.sh >> /var/log/infra2-backup-quarterly.log 2>&1
```

> **OFF-HOST STATUS**: **ACTIVE**. Off-host logical backups are encrypted end-to-end
> (`rclone crypt`) and uploaded to Google Drive (`gdrive-backup:infra2`).
> The root of trust is 1Password (`bootstrap/gdrive`). To restore credentials on a
> fresh host: `op item get "bootstrap/gdrive" --vault "Infra2" --fields "rclone_conf" > ~/.config/rclone/rclone.conf`.
> The off-host manifest is verified with SOP-004.

### SOP-006A: Off-host restore rehearsal

Backups are not considered durable until the latest off-host artifact has been
restored into a disposable target and checked. Use
`tools/run_restore_rehearsal.py` for automated, sandboxed end-to-end rehearsals:

```bash
# Automated sandboxed rehearsal (spins up disposable container, restores, checks invariants, destroys container)
python3 tools/run_restore_rehearsal.py \
  --manifest /data/backups/infra2/production-manifest.json \
  --service-id finance_report/postgres \
  --database finance_report
```

Or low-level manual invocation with `tools/backup_restore_rehearsal.py`:

```bash
uv run python tools/backup_restore_rehearsal.py \
  --manifest /data/backups/infra2/production-manifest.json \
  --service-id finance_report/postgres \
  --target-container finance_report-postgres-restore-rehearsal
```

Safety rules:

- The manifest must pass the same off-host freshness/checksum checks as SOP-004.
- The automated runner defaults to Production and requires the manifest's
  `environment` to match. To rehearse Staging, pass both `--environment staging`
  and `--manifest /data/backups/infra2/staging-manifest.json`. Missing or
  mismatched environment is a failure; the runner never guesses from the latest
  timestamp because the later Staging cron would otherwise mask Production.
- The target container name must contain `rehearsal`, `restore`, or `throwaway`
  unless an operator deliberately uses the explicit override in code.
- Zero host port binds and zero volume mounts to production `/data`.
- The default invariants run `SELECT 1`,
  `SELECT count(*) >= 1 FROM pg_database`, table count assertions, and domain
  row count checks (e.g. `SELECT count(*) FROM accounts`, `alembic_version`).

Recommended schedule after the rehearsal target is provisioned:

```cron
15 4 * * 0 BACKUP_REMOTE=gdrive-backup:infra2 python3 /infra2/tools/run_restore_rehearsal.py >> /var/log/infra2-backup-restore-rehearsal.log 2>&1
```

### SOP-007: 服务因 vault-agent 缺凭证崩溃 (re-provision AppRole)

- **触发条件**: 某服务的 vault-agent 反复 `Restarting`,日志 `VAULT_ROLE_ID and
  VAULT_SECRET_ID are required`(旧 token_file 服务则是 `VAULT_APP_TOKEN is
  required`);它的 app 容器停在 `created`/`unhealthy`,公网路由 404。最常见于:
  服务被 recreate(一次部署 / AppRole 迁移落地)后,Dokploy 项目 env 里少了这两个 key。
- **先确认不是应用的锅**: backend 没启动 → 迁移没跑 → DB/ODS 安全。证据:该服务
  `…/10.app/.env`(或对应目录)缺 `VAULT_ROLE_ID`/`VAULT_SECRET_ID`;iac-runner
  sync 日志出现 `vault_permission_denied`。
- **恢复**(在 iac-runner 里跑——它已带 vault CLI(#289)且能从 1Password 取 root token):
    ```bash
    ssh root@<VPS_HOST>
    docker exec iac-runner sh -c '
      set -a; . /secrets/.env 2>/dev/null; set +a
      export VAULT_ROOT_TOKEN=$(op read "op://Infra2/<vault-root-token-item>/Token")
      cd /workspace/infra2
      BOOT="import platform, runpy, sys; sys.path.insert(0, \".\"); runpy.run_module(\"invoke\", run_name=\"__main__\")"
      python3 -P -c "$BOOT" vault.setup-approle --project <project> --service <service> --deploy
    '
    ```
  `setup-approle --deploy` 会:幂等启用 approle → 建/取 role + 签发 secret-id → 写回
  Dokploy 项目 env(#294 的 `RUNTIME_ENV_KEYS_TO_PRESERVE` 保证之后重部署不再抹掉)→
  触发重部署。**全程不要 echo/打印 token。**
- **兜底**(Dokploy 重部署没产生新 deployment record / 高负载): 直接把 role-id +
  fresh secret-id 写进该服务 `.env`,`docker compose -p <proj> -f <compose> up -d`
  重建(creds `secret_id_ttl=0` 不过期)。
- **验证**: vault-agent `healthy` → app 容器起 → `/api/health` 200 → 迁移落地。
- **关联**: 根因 #290(provisioning 链脆弱);creds 持久化 #294;vault CLI 入镜像
  #289;policy 缺口 #287。**收尾目标(高优)**: 把这条 playbook 自动化成不依赖人肉
  root token 的可重放 provisioning,见 #290。


---

## 5. 验证与测试 (The Proof)

| 行为描述 | 验证方式 | 覆盖率 |
|----------|----------|--------|
| **Backup inventory covers DATA_PATH** | `libs/tests/test_backup_verification.py` | ✅ Implemented |
| **Backup archive + checksum runner** | `tools/backup_runner.py` | ✅ Implemented |
| **Backup freshness/checksum manifest** | `tools/backup_verification.py` | ✅ Implemented |
| **On-host backup runner (SOP-006): dumps first, one failure never stops the rest, tar exit 1 is a WARN, authenticated redis SAVE** | `libs/tests/test_host_backup_script.py` | ✅ Implemented |
| **Off-host restore rehearsal** | `tools/run_restore_rehearsal.py` + `libs/backup/` (`libs/tests/test_backup_verification.py`) | ✅ Implemented & Live Verified (10.62s PASS) |
| **Vault Unseal 流程（自动）** | `bootstrap/05.vault/unsealer.py` 常驻自动解封;契约由 `libs/tests/test_vault_unsealer.py`(过期 Connect token 拒绝 / sync 非 ACTIVE / sealed 报不健康 / key 不足中止)+ `libs/tests/test_bootstrap_health.py`(healthcheck 接线)覆盖 | ✅ Automated |
| **Vault Unseal 流程（手动兜底,SOP-001）** | `vault status` + `vault operator unseal` | ✅ Manual |
| **vault-agent 凭证 re-provision (SOP-007)** | `vault.setup-approle --deploy` | ✅ Manual |

---

## Used by

- [docs/ssot/README.md](./README.md)
- [docs/onboarding/README.md](../onboarding/README.md)
