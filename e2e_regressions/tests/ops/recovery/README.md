# Recovery Tests

验证系统在组件故障时的恢复路径。

## 📚 SSOT References

本测试套件是以下文档的 **Test Anchor**：
> [**Recovery SSOT**](../../../../docs/ssot/ops.recovery.md#5-验证与测试-the-proof)

## 测试矩阵

| 组件 | 测试 | 验证内容 |
|------|------|----------|
| **Vault** | `test_vault_unseal` | Unseal 流程可重复性 |
| **Backup** | `test_backup_exists` | 备份文件在宿主机存在性 |

## 运行测试

```bash
uv run pytest tests/ops/recovery/ -v
```
