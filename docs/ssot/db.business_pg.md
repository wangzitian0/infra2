# Business PostgreSQL SSOT

> **SSOT Key**: `db.business_pg`
> **核心定义**: 业务应用主数据库的配置规范、连接信息及动态凭据策略。

---

## 1. 真理来源 (The Source)

本话题的配置和状态由以下物理位置唯一确定：

| 维度 | 物理位置 (SSOT) | 说明 |
|------|----------------|------|
| **实例定义** | [`envs/data-shared/1.postgres.tf`](../../envs/data-shared/1.postgres.tf) | 数据库集群定义 |
| **运行时密钥** | **Vault** (`secret/data/postgres`) | 静态 Master 密码 |
| **动态角色** | **Vault** (`database/creds/app-*`) | 动态生成的应用凭据 |

---

## 2. 架构模型

```mermaid
graph LR
    APP[L4 App + Vault Agent] -->|"Read database/creds/app-readonly"| VAULT[Vault]
    VAULT -->|"GRANT SELECT"| PG[(L3 Business PG)]
    VAULT -->|"Dynamic Creds"| APP
```

---

## 3. 设计约束 (Dos & Don'ts)

### ✅ 推荐模式 (Whitelist)

- **模式 A**: 应用程序应优先使用 `app-readonly` 角色进行非破坏性查询。
- **模式 B**: 定期运行 `SOP-001` 验证数据库健康。

### ⛔ 禁止模式 (Blacklist)

- **反模式 A**: **禁止** 业务应用直接连接 L1 Platform PG。
- **反模式 B**: **禁止** 手动在数据库中创建用户（所有用户应由 Vault 托管）。

---

## 4. 标准操作程序 (Playbooks)

### SOP-001: 数据库连接测试

- **执行者**: 开发者/运维
- **步骤**:
    1. 获取临时凭据: `vault read database/creds/app-readonly`
    2. 连接测试:
       ```bash
       psql -h postgresql.data-staging.svc.cluster.local -U <username> -d business
       ```

### SOP-002: 数据库备份与恢复

- **触发条件**: 数据迁移 / 灾难恢复
- **步骤**:
    1. **备份**:
       ```bash
       NS="data-staging" # or data-prod
       kubectl exec -n "$NS" postgresql-0 -- pg_dump -U postgres app > l3_backup.sql
       ```
    2. **恢复**:
       ```bash
       kubectl exec -n "$NS" postgresql-0 -- psql -U postgres -d app < l3_backup.sql
       ```

---

## 5. 验证与测试 (The Proof)

| 行为描述 | 测试文件 (Test Anchor) | 覆盖率 |
|----------|-----------------------|--------|
| **PG 集群健康状态** | [`test_postgresql.py`](../../e2e_regressions/tests/data/postgresql/test_postgresql.py) | ✅ Critical |

---

## Used by

- [docs/ssot/db.overview.md](./db.overview.md)
- [e2e_regressions/tests/data/postgresql/README.md](../../e2e_regressions/tests/data/postgresql/README.md)
