# Handover — 2026-09-09

上一段工作的交接。事实都在写的时候现场核验过，命令可直接复制运行。

---

## 一句话状态

秘密供应链（#649）已经收口：Vault 只有一个写入者，每日 reconcile 会自己发现漂移；
过程中发现并已修复一个真实的权限问题（三个应用在用 MinIO root 凭证），两个 MinIO root 口令已轮换。
现在卡在三个"等人点头"的地方：两个应用仓的 PR、infra2 的下一个发布 tag。

---

## 一、坐标

| 仓库 | main | 最新 tag | 子模块指针 | 线上 |
|---|---|---|---|---|
| infra2 | `61b64b8` | **v1.1.76** = `4c81e30` | — | runner 跑 v1.1.76 |
| infra2-sdk | `50439e3` | **v1.5.1** = `50439e3` | `1b8179f`（落后一个提交） | infra2 pin 1.5.1 |
| truealpha | `effdae9` | v0.0.49 = `95bab22` | `a3c7afe` | data-engine 还是旧镜像 `sha256:a6a6e305`（sdk 1.3.2）；web v0.0.48 |
| finance_report | `4340f76b` | v0.1.48 | `4340f76b` | 生产 v0.1.45，staging v0.1.48 |

infra2 main 上有 **两个提交还没进 tag**：`4ebe5d4`（AGENTS.md 合流权限）、`61b64b8`（#678 最小权限检查）。
**在 infra2 里推 tag 才会触发部署**（`reconcile-iac-inputs.yml` 只在 push tag 时跑），合并到 main 只跑 CI。

---

## 二、今天做完的

### 1. SDK 收敛：三个仓第一次钉同一个版本
`infra2-sdk` 发了 **v1.5.0**（manifest 驱动器、`VaultKvBackend(write_mode)`、`vault_token_status`、
registry digest 挑战认证、`canonical_sha256`）和 **v1.5.1**（`VaultKvBackend.replace`，用 create/update
整份重写文档，不需要 delete 权限）。三个消费方各删掉一份重复实现：

- infra2：`UpdateOnlyVaultKv`、手写 httpx 的 `verify_vault_token`、`libs/image_digest.py` 全删。
- truealpha：`tools/env_manifest.py` 变成薄壳；digest 解析走 SDK；canary 那个错钉 1.2.0 的常量修正。
- finance_report：生成器里手写的 source-class fold 删除，改用 SDK 的 `overrides=`。

### 2. 环境/运行时清理（infra2 v1.1.73–v1.1.76）
- 配置哈希不再丢空值（变量变成 `""` 以前不会触发重部署）。
- prefect / authentik / openpanel 的 root-token 门禁改读 `VAULT_TOKEN`。
- `invoke env.* --type` 生效（所有 SSOT 文档写的都是这个拼写）。
- `libs.common.set_deploy_env()`：修掉 drift 扫描把非生产环境算成生产哈希的 bug。
- **存储卫生**：基类 Deployer 不再与 supply 并行生成 `secret_key`（默认值 `"password"` 就是 alerting
  那个孤儿键的来源）；运维专用、故意不渲染进容器的键在注册表里显式声明；新增 `tools/secrets_prune.py`。

### 3. MinIO 最小权限（infra2 #677，已关闭）
**这是今天最严重的发现。** finance_report 在**两个环境**都用生产 MinIO 的 root 凭证，
truealpha/staging/data_engine 用 staging root。即 finance_report 能读写删除生产实例上的任何桶。

已处置并逐项验证：
- 建了按桶授权的用户：`finance_report`→`statements`、`finance_report_staging`→`finance-report-staging`、
  data_engine 复用它自家 app 的 `truealpha_raw`。
- 每个消费方重启后在容器内实测 `head/put/get/delete` 通过、跨桶被拒；公网 `report.zitian.party/api/health` 200。
- **两个 MinIO root 口令都已轮换**，并同步回 1Password（`platform/minio/admin`、`platform/minio/admin-staging`，
  哈希比对确认一致）。全量扫描 37 个 Vault 路径，root 口令现在只存在于 `platform/{env}/minio`。
- 机制层（#678 已合入 main）：reconcile 新增 `over_privileged` 并**会告警**；`create_app_bucket` 改用
  `mc alias import local /dev/stdin`（镜像自带的 `local` 别名 access key 是空的，导致这个工具一直静默失败，
  这才是有人手工配 root 的根因）。

⚠️ 核实过程中有一条命令把那个 root 口令回显进了本次会话记录，这也是立刻轮换的原因之一。

### 4. Cloudflare 配额
根因不是流量：线上 watchdog worker 停在 6 月 14 日，比 7 月合并 KV 写入的代码旧。已 `wrangler deploy`。
KV 写入 **09-07 = 1182（超额）→ 09-08 = 659（正常）**。
注意 reconcile 默认看"昨天"，所以它还会再报一天 exceeded，之后自动转绿。

---

## 三、等你处理（按重要性）

### A. 两个应用仓的 PR（agent 不合并，规则如此）

| PR | 状态 | 内容 |
|---|---|---|
| [finance_report #2033](https://github.com/wangzitian0/finance_report/pull/2033) | MERGEABLE / CLEAN，21 项通过 | 禁止随仓产物给别名链字段赋空值（manifest 驱动的门禁，额外抓出 `LLM_ENCRYPTION_KEYS`） |
| [truealpha #786](https://github.com/wangzitian0/truealpha/pull/786) | MERGEABLE / CLEAN，20 项通过 | `RELEASE_MANIFEST_ID` 从常量哈希改成真实测量；data-engine 镜像开始携带环境契约 |

truealpha #786 按它自己的规矩还差一步：最终 head 的 re-review 尚未提交，review 流程未算完成。

### B. infra2 的下一个 tag（这是部署动作，留给你）
main 上未发布的 `61b64b8` 带着 `over_privileged` 检查——**不打 tag，每日 reconcile 就跑不到它**。

```bash
cd ~/zitian/infra2-datahub && git fetch --tags
git tag -a v1.1.77 -m "v1.1.77: over_privileged check; mc alias import (#677)" origin/main
git push origin v1.1.77     # 推 tag = 触发 staging 晋升
```

### C. infra2 #679（我开的，等 review）
prune 工具的两个缺陷修复：不删 `_` 前缀的探针键；不删 Deployer 在部署时会读的键
（`data_engine:GIT_COMMIT_SHA`、两个 app 的 `S3_BUCKET`）。带一张表把每个服务的这类读取都写进测试。

---

## 四、下一步（有先后依赖）

1. **执行 prune**（等 #679 合并 + 打 tag 后，runner 才有修好的工具）：
   ```bash
   ssh root@103.214.23.41 'docker exec iac-runner sh -c "set -a; . /secrets/.env; set +a; \
     cd /workspace/infra2 && python3 tools/secrets_prune.py"'          # 先 dry run
   # 确认无误后加 --apply；KV v2 保留历史版本，可 vault kv rollback
   ```
   今晚的 dry run：13 条路径、85 个孤儿键。**每一个都已逐条比对过线上正在用的 Vault Agent 模板，
   确认没有任何一个仍被渲染**——这是敢执行的依据。执行完 reconcile 的 `unclassified` 应该清零。

2. **接上 data-engine 的开机校验**（infra2 侧）：truealpha #786 合并并出一次镜像后，
   在 `truealpha/truealpha/20.data_engine` 的 entrypoint 里调
   `truealpha_runtime.boot.assert_environment`。今天验证过：**当前镜像里没有 manifest**，所以现在接会失败。

3. **发应用版本**：FR 生产落后 3 个版本且镜像里根本没有 sdk；data-engine 的 canary 断言现在必然红
   （镜像里 sdk 1.3.2，代码钉 1.2.0）。各自出一个 release 就能同时解决。

4. **finance_report 空值的持久修复**：[FR #2032](https://github.com/wangzitian0/finance_report/issues/2032)。
   需要先解决"exact consumer proof 从哪来、在 CI 哪一级消费"，不要绕过 DDD 门禁——门禁的判断是对的。

5. **finance_report staging 的对象存储隔离**：现在是"生产实例上的独立桶 + 独立最小权限用户"。
   彻底隔离要建桶建用户、搬 176K 数据、公网端点切到 `s3-staging.zitian.party`（两条公网路由都验过 200）。

6. **遗留**：`appwrite` 这个 MinIO 账号带 `consoleAdmin`（在本套注册表之外，需要你决定）；
   FR 应该退役 `S3_PUBLIC_ACCESS_KEY`/`S3_PUBLIC_SECRET_KEY` 两个死字段（今天已置空）。

---

## 五、踩过的坑（下次直接用）

- **推 tag 前先 `git fetch --tags`**。今天 v1.1.63/64 被另一条线同时占用；v1.1.77 我一度打到了别人的提交上，
  只能删掉重来。truealpha 的 release protocol 说得对：先抢版本号再干活。
- **vault-agent 不会自动重渲染**。改了 Vault 的值之后必须 `docker restart <agent>`，
  再重启应用容器（应用只在启动时读 `/secrets/.env`）。
- **`docker exec` 拿不到应用的环境变量**：entrypoint 是 `set -a; . /secrets/.env` 注入 PID1 的，
  新 exec 进程没有。要 `docker exec X sh -c 'set -a; . /secrets/.env; set +a; ...'`。
- **凭证只走 stdin**。读 Vault → 管道 → `docker exec -i`，不要出现在 argv（宿主和容器的进程表都看得到）。
  比对凭证一律用 sha256 前缀，不要打印。
- **`op` 命令要 `stdin=DEVNULL`**。否则它会把 heredoc 剩余内容当 JSON 读，报 `invalid JSON provided`。
- **GitGuardian 扫的是 PR 里的每个提交，不是最终树**。测试里写 `root_password = "字面量"` 会被拦；
  已经提交过就得 squash 掉那个提交，改一版在后面是没用的。
- **MinIO 镜像自带的 `mc` 别名 `local` access key 是空的**，所有 `mc admin` 都会 Access Denied。
  用 `mc alias import local /dev/stdin` 喂 JSON 凭证。
- **infra2 的 AGENTS.md 现在有"会话级合流授权"**（owner 2026-09-08 批准）：全绿 + review 闭环 +
  距最后一次 push **静置 12 分钟** + 不碰受保护文件 + 合并本身不触发部署，才可自行合流。
  触发部署的、改受保护文件的，仍需逐-head 批准。
- **finance_report 的 AGENTS.md 明确禁止 agent 合并 PR**，交付物就是绿 CI 的 PR。
  （今天早些时候我合了 FR #2028，之后才读到这条；而且 squash 用的是 PR 原始描述，
  声称了第二个提交已经撤掉的 `env_ignore_empty`——已记在 FR #2032 里。）

---

## 六、常用命令

```bash
# 每日核对（在 runner 里跑，只报名字不报值）
ssh root@103.214.23.41 'docker exec iac-runner sh -c "set -a; . /secrets/.env; set +a; \
  cd /workspace/infra2 && python3 tools/secrets_reconcile.py"'

# 手工触发那个定时任务（端到端，含 SSH 与告警链路）
gh workflow run ops-checks.yml -f task=secrets-reconcile -f dry_run=true

# 部署（staging 需要 app 的 tag，不接受 main）
gh workflow run deploy.yml -f service=finance_report/app -f type=staging \
  -f version_ref=v0.1.48 -f iac_ref=v1.1.76

# 线上环境变量比对（VPS 上，只比名字和值的哈希）
ssh root@103.214.23.41 'bash /root/env-diff2.sh production'   # 或 staging
```

当前 reconcile：**18 ok / 9 条有发现**，全部是待 prune 的孤儿键，没有 missing / empty / stale / over_privileged。
