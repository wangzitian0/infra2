# Bootstrap 网络层测试

验证 DNS 解析和 TLS 证书配置。

## SSOT 参考

- [bootstrap.network.md](../../../../docs/ssot/bootstrap.network.md)

## 测试矩阵

| 组件 | 测试 | 标记 | 验证内容 |
|------|------|------|----------|
| **DNS** | `test_dns_resolution_portal` | smoke | Portal 域名解析 |
| **DNS** | `test_dns_resolution_all_services` | - | 所有服务域名解析 |
| **DNS** | `test_dns_wildcard_subdomain` | - | 通配符 DNS |
| **DNS** | `test_dns_consistency` | - | DNS 一致性 |
| **DNS** | `test_dns_k3s_api_resolvable` | - | K3s API 域名 (Grey) |
| **TLS** | `test_certificates_https_enabled` | smoke | HTTPS 启用 |
| **TLS** | `test_certificates_valid_or_self_signed` | - | 证书有效性 |
| **TLS** | `test_certificate_expiry_check` | - | 证书过期检查 |
| **TLS** | `test_certificate_issuer_info` | - | 证书颁发者信息 |
| **TLS** | `test_cert_manager_issuer_configured` | - | cert-manager 配置 |

## 运行测试

```bash
# 所有网络层测试
uv run pytest tests/bootstrap/network_layer/ -v

# Smoke 测试
uv run pytest tests/bootstrap/network_layer/ -m smoke -v
```

## 环境变量

| 变量 | 必需 | 说明 |
|------|------|------|
| `PORTAL_URL` | ✅ | Portal URL |
| `SSO_URL` | ✅ | SSO URL |
| `VAULT_URL` | ✅ | Vault URL |
| `DASHBOARD_URL` | ✅ | Dashboard URL |
