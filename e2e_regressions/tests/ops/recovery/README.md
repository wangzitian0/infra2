# Recovery Tests

验证系统在组件故障时的恢复路径。

## 📚 SSOT References

本测试套件是以下文档的 **Test Anchor**：
> [**Recovery SSOT**](../../../../docs/ssot/ops.recovery.md#5-验证与测试-the-proof)

## 测试矩阵

| 组件 | 测试 | 验证内容 |
|------|------|----------|
| **Storage** | `test_recovery_storage_policy` | Retain 策略存在性 |
| **Docs** | `test_recovery_docs_exist` | 恢复流程文档存在 |

## 运行测试

```bash
uv run pytest tests/ops/recovery/ -v
```
