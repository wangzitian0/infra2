# Application Layer Tests

验证 L4 应用的健康状态、API 可用性及 SSO 集成。

## 📚 SSOT References

本测试套件关联以下 SSOT：
- [**Auth SSOT**](../../../docs/ssot/platform.auth.md)
- [**Vault Integration**](../../../docs/ssot/db.vault-integration.md)

## 测试矩阵

| 应用 | 测试 | 验证内容 |
|------|------|----------|
| **Backend** | `test_api_health` | GraphQL API 连通性 |
| **Portal** | `test_portal_sso` | Portal 登录重定向 |

## 运行测试

```bash
uv run pytest tests/apps/ -v
```
