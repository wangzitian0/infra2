# Bootstrap E2E Tests

验证 Bootstrap 层基础设施的端到端测试（Dokploy + 基础服务）。

## 📚 SSOT References

这些测试用于验证以下文档中定义的行为：

- **Compute**: [核心架构 SSOT / 验证与测试](../../../docs/ssot/core.md#6-验证与测试-the-proof)
- **Storage**: [运维存储 SSOT / 验证与测试](../../../docs/ssot/ops.storage.md#5-验证与测试-the-proof)
- **Network**: [核心架构 SSOT / 验证与测试](../../../docs/ssot/core.md#6-验证与测试-the-proof)

## 测试结构

| 层级 | 目录 | 覆盖内容 |
|------|------|----------|
| **计算** | `compute/` | Dokploy 可达性、基础服务路由 |
| **存储** | `storage_layer/` | /data 挂载路径、DB 可达性（可选） |
| **网络** | `network_layer/` | DNS、TLS 证书 |

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

## Smoke Tests (关键路径)

| 组件 | 测试 | 验证 |
|------|------|------|
| Dokploy | `test_dokploy_ui_accessible` | 服务可达 |
| Storage | `test_bootstrap_data_paths_defined` | /data 挂载 |
| DNS | `test_dns_resolution_core_services` | 域名解析 |
| TLS | `test_certificates_https_enabled` | HTTPS 启用 |

## 环境变量

| 变量 | 必需 | 说明 |
|------|------|------|
| `DOKPLOY_URL` | ❌ | Dokploy URL（默认 cloud.<domain>） |
| `OP_URL` | ❌ | 1Password URL（默认 op.<domain>） |
| `VAULT_URL` | ❌ | Vault URL（默认 vault.<domain>） |
| `SSO_URL` | ❌ | Authentik URL（默认 sso.<domain>） |
| `INTERNAL_DOMAIN` | ✅ | Internal domain |
