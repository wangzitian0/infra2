# Platform Auth Tests

验证 Authentik SSO 服务的可达性与基础 OIDC 配置。

## 测试矩阵

| 组件 | 测试 | 说明 |
|------|------|------|
| **Authentik** | `test_authentik_health` | 健康检查 |
| **Authentik** | `test_authentik_login_page_loads` | UI 可达 |
| **OIDC** | `test_oidc_discovery_endpoint` | OIDC 发现端点（可选） |

## 环境变量

| 变量 | 必需 | 说明 |
|------|------|------|
| `OIDC_DISCOVERY_URL` | ❌ | OIDC 发现端点（如配置） |

## 运行测试

```bash
uv run pytest tests/platform/auth/ -v
```
