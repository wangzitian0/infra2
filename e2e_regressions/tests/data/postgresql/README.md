# PostgreSQL Tests

验证业务 PostgreSQL 数据库的连接和基本操作。

## 📚 SSOT References

本测试套件是以下文档的 **Test Anchor**：
> [**Business PostgreSQL SSOT**](../../../../docs/ssot/db.business_pg.md#5-验证与测试-the-proof)

## 测试矩阵

| 组件 | 测试 | 标记 | 验证内容 |
|------|------|------|----------|
| **Connectivity** | `test_pg_connection` | smoke | 基本连接可达性 |
| **Auth** | `test_pg_auth` | critical | 静态/动态凭据登录 |
| **Persistence** | `test_pg_persistence` | - | 数据持久化验证 |

## 运行测试

```bash
uv run pytest tests/data/postgresql/ -v
```

## 环境变量

| 变量 | 必需 | 说明 |
|------|------|------|
| `DB_HOST` | ✅ | 数据库地址 |
| `DB_USER` | ✅ | 用户名 |
| `DB_PASSWORD` | ✅ | 密码 |