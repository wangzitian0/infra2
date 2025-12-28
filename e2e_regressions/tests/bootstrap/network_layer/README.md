# Bootstrap Network Tests

验证 DNS 与 TLS 证书配置。

## 测试矩阵

| 组件 | 测试 | 说明 |
|------|------|------|
| DNS | `test_dns_resolution_core_services` | 核心域名解析 |
| DNS | `test_dns_wildcard_subdomain` | 通配解析 |
| TLS | `test_certificates_https_enabled` | HTTPS 启用 |
| TLS | `test_certificates_valid_or_self_signed` | 证书有效性 |

## 运行测试

```bash
uv run pytest tests/bootstrap/network_layer/ -v
```
