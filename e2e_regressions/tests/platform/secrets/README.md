# Platform Secrets Tests

验证 Vault 密钥管理、读写权限及注入机制。

## 📚 SSOT References

本测试套件是以下文档的 **Test Anchor**：
> [**Platform Secrets SSOT**](../../../../docs/ssot/platform.secrets.md#5-验证与测试-the-proof)

## 测试矩阵

| 组件 | 测试 | 标记 | 验证内容 |
|------|------|------|----------|
| **Vault** | `test_vault_health` | smoke | 服务健康及 Unseal 状态 |
| **KV Engine** | `test_kv_read_write` | critical | 密钥读写能力 |
| **Loader** | `test_secrets_loader` | unit | 1Password -> GitHub 映射逻辑 |

## 运行测试

```bash
# E2E Tests
uv run pytest tests/platform/secrets/ -v

# Loader Unit Tests
python3 ../../../tools/secrets/tests/test_secrets_loader.py
```

## 环境变量

| 变量 | 必需 | 说明 |
|------|------|------|
| `VAULT_URL` | ✅ | Vault 地址 |
| `VAULT_TOKEN` | ✅ | 测试用 Token (需有读写权限) |
