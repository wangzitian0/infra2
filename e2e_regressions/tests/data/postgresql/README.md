# PostgreSQL Tests

验证业务 PostgreSQL 数据库的连接和基本操作。

## Scope

This suite connects to one PostgreSQL server. The environment variables below select the server. Both tests skip when the host or the password is missing. `PG_HOST` and `PG_PASS` take precedence over `DB_HOST` and `DB_PASSWORD`.

## 测试矩阵

| 组件 | 测试 | 标记 | 验证内容 |
|------|------|------|----------|
| **Connectivity** | `test_postgresql_connect` | database | 基本连接可达性 |
| **Version** | `test_postgresql_version` | database | 版本信息可读 |

## 运行测试

```bash
uv run pytest tests/data/postgresql/ -v
```

## 环境变量

| 变量 | 必需 | 说明 |
|------|------|------|
| `DB_HOST` | ✅ | 数据库地址 |
| `DB_PORT` | ❌ | 端口 (默认 5432) |
| `DB_USER` | ❌ | 用户名 (默认 postgres) |
| `DB_PASSWORD` | ✅ | 密码 |
