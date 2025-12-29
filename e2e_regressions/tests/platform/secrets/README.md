# Platform Secrets Tests

验证 Vault 服务健康与密封状态。

## 📚 SSOT References

本测试套件是以下文档的 **Test Anchor**：
> [**Bootstrap Vars & Secrets SSOT**](../../../../docs/ssot/bootstrap.vars_and_secrets.md)

## 测试矩阵

| 组件 | 测试 | 标记 | 验证内容 |
|------|------|------|----------|
| **Vault** | `test_vault_health` | platform | 健康与初始化状态 |
| **Vault** | `test_vault_seal_status` | platform | Seal 状态端点可达 |

## 运行测试

```bash
uv run pytest tests/platform/secrets/ -v
```

## 环境变量

| 变量 | 必需 | 说明 |
|------|------|------|
| `VAULT_URL` | ✅ | Vault 地址 |
