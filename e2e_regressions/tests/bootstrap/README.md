# Bootstrap E2E Tests

验证 Bootstrap 层基础设施的端到端测试。

## 📚 SSOT References

这些测试用于验证以下文档中定义的行为：

- **Compute**: [Bootstrap Compute SSOT / 验证与测试](../../../docs/ssot/bootstrap.compute.md#5-验证与测试-the-proof)
- **Storage**: [Bootstrap Storage SSOT / 验证与测试](../../../docs/ssot/bootstrap.storage.md#5-验证与测试-the-proof)
- **Network**: [Bootstrap Network SSOT / 验证与测试](../../../docs/ssot/bootstrap.network.md#5-验证与测试-the-proof)

## 测试结构

| 层级 | 目录 | 测试数 | 覆盖内容 |
|------|------|--------|----------|
| **计算** | `compute/` | 11 | K3s, Digger, Traefik |
| **存储** | `storage_layer/` | 8 | StorageClass, Platform PG |
| **网络** | `network_layer/` | 10 | DNS, TLS Certificates |

## 运行测试

```bash
cd e2e_regressions

# 所有 Bootstrap 测试
uv run pytest tests/bootstrap/ -v

# 按层运行
uv run pytest tests/bootstrap/compute/ -v
uv run pytest tests/bootstrap/storage_layer/ -v
uv run pytest tests/bootstrap/network_layer/ -v

# Smoke 测试 (核心用例)
uv run pytest tests/bootstrap/ -m smoke -v
```

## 测试矩阵总览

### Smoke Tests (关键路径)

| 组件 | 测试 | 验证 |
|------|------|------|
| K3s | `test_k3s_api_accessible` | API 可达 |
| Digger | `test_digger_endpoint_accessible` | 服务可达 |
| Traefik | `test_traefik_routes_traffic` | 路由功能 |
| StorageClass | `test_storage_class_local_path_retain_defined` | 定义存在 |
| Platform PG | `test_platform_pg_config_exists` | 配置存在 |
| DNS | `test_dns_resolution_portal` | 域名解析 |
| TLS | `test_certificates_https_enabled` | HTTPS 启用 |

## 环境变量

| 变量 | 必需 | 说明 |
|------|------|------|
| `PORTAL_URL` | ✅ | Portal URL |
| `SSO_URL` | ✅ | SSO URL |
| `VAULT_URL` | ✅ | Vault URL |
| `DASHBOARD_URL` | ✅ | Dashboard URL |
| `PLATFORM_DB_*` | ❌ | Platform PG 连接 (可选) |