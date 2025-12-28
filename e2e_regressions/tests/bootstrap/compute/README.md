# Bootstrap Compute Tests

验证 Dokploy 与核心入口服务的可达性。

## 测试矩阵

| 组件 | 测试 | 说明 |
|------|------|------|
| Dokploy | `test_dokploy_ui_accessible` | UI 可达 |
| Bootstrap Services | `test_bootstrap_services_accessible` | 1Password/Vault/SSO | 
| HTTPS Redirect | `test_https_redirect_or_https_only` | HTTP 重定向或关闭 |
| Headers | `test_proxy_headers_present` | 基础响应头 |

## 运行测试

```bash
uv run pytest tests/bootstrap/compute/ -v
```
