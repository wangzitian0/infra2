# Bootstrap Storage Tests

验证 /data 目录挂载与基础存储约束。

## 测试矩阵

| 组件 | 测试 | 说明 |
|------|------|------|
| /data | `test_bootstrap_data_paths_defined` | Bootstrap 挂载 |
| /data | `test_platform_data_paths_defined` | Platform 挂载 |
| DB | `test_platform_pg_accessible` | Postgres 可达（可选） |

## 运行测试

```bash
uv run pytest tests/bootstrap/storage_layer/ -v
```
