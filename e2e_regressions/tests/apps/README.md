# Application Layer Tests

验证 L4 应用的健康状态、API 可用性及 SSO 集成。

## 📚 SSOT References

本测试套件关联以下 SSOT：
- [**SSO SSOT**](../../../docs/ssot/platform.sso.md)
- [**Vault Integration**](../../../docs/ssot/db.vault-integration.md)

## 测试矩阵

| 应用 | 测试 | 验证内容 |
|------|------|----------|
| **Core Services** | `test_api_health` | 核心服务连通性 |
| **Portal** | `test_portal_sso` | Portal 登录重定向（可选） |
| **Finance Report** | `test_finance_report_health_endpoint`, `test_finance_report_frontend_available` | 应用健康检查与依赖状态 |

## 运行测试

```bash
uv run pytest tests/apps/ -v
```
