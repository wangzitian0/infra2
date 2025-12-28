# Business PostgreSQL SSOT

> **SSOT Key**: `db.business_pg`
> **核心定义**: 业务 PostgreSQL（规划中）。

---

## 1. 现状

- 本仓库尚未包含 Business PG 的部署代码。
- 相关实现与接入规范待补齐。

---

## 2. 计划方向

- 采用 Docker Compose + Dokploy 管理数据库实例。
- 密钥统一存入 Vault (`secret/<project>/<env>/postgres`)。

---

## 3. 验证与测试 (The Proof)

| 行为描述 | 验证方式 | 状态 |
|----------|----------|------|
| **业务 PG 接入验证** | 待补测试用例 | ⏳ Backlog |

---

## Used by

- [docs/ssot/db.overview.md](./db.overview.md)
