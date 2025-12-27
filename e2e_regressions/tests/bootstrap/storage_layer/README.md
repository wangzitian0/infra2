# Bootstrap 存储层测试

验证 StorageClass 配置和 Platform PostgreSQL 健康状态。

## SSOT 参考

- [bootstrap.storage.md](../../../../docs/ssot/bootstrap.storage.md)

## 测试矩阵

| 组件 | 测试 | 标记 | 验证内容 |
|------|------|------|----------|
| **StorageClass** | `test_storage_class_local_path_retain_defined` | smoke | StorageClass 定义 |
| **StorageClass** | `test_storage_class_reclaim_policy` | - | Retain 策略 |
| **StorageClass** | `test_storage_data_directory_configured` | - | /data 目录配置 |
| **StorageClass** | `test_storage_provisioner_configured` | - | Provisioner 配置 |
| **Platform PG** | `test_platform_pg_config_exists` | smoke | 配置文件存在 |
| **Platform PG** | `test_platform_pg_accessible` | - | 数据库可连接 |
| **Platform PG** | `test_platform_pg_databases_exist` | - | Vault/Casdoor DB 存在 |
| **Platform PG** | `test_platform_pg_namespace_configured` | - | Namespace 配置 |

## 运行测试

```bash
# 所有存储层测试
uv run pytest tests/bootstrap/storage_layer/ -v

# Smoke 测试
uv run pytest tests/bootstrap/storage_layer/ -m smoke -v
```

## 环境变量

| 变量 | 必需 | 说明 |
|------|------|------|
| `PLATFORM_DB_HOST` | ❌ | Platform PG 主机 |
| `PLATFORM_DB_PORT` | ❌ | Platform PG 端口 (默认 5432) |
| `PLATFORM_DB_USER` | ❌ | Platform PG 用户 (默认 postgres) |
| `PLATFORM_DB_PASSWORD` | ❌ | Platform PG 密码 |
