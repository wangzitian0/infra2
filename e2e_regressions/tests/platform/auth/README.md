# Platform Auth Tests

验证 SSO、OAuth2、Casdoor 认证流程。

## 📚 SSOT References

本测试套件是以下文档的 **Test Anchor**：
> [**Platform Auth SSOT**](../../../../docs/ssot/platform.auth.md#5-验证与测试-the-proof)

## 测试矩阵

| 组件 | 测试 | 标记 | 验证内容 |
|------|------|------|----------|
| **Casdoor** | `test_casdoor_health` | smoke | 服务健康状态 |
| **Casdoor** | `test_oidc_discovery` | - | OIDC 配置端点 |
| **Flow** | `test_login_flow` | critical | 完整登录流程 |
| **RBAC** | `test_role_mapping` | - | Casdoor Role -> Vault Policy 映射 |

## 运行测试

```bash
uv run pytest tests/platform/auth/ -v
```

## 环境变量

| 变量 | 必需 | 说明 |
|------|------|------|
| `SSO_URL` | ✅ | Casdoor URL |
| `TEST_USERNAME` | ✅ | 测试账号 |
| `TEST_PASSWORD` | ✅ | 测试密码 |