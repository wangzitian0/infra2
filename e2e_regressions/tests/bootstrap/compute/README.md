# Bootstrap 计算层测试

验证 K3s 集群、Digger CI 和 Traefik Ingress 的健康状态。

## 📚 SSOT References

本测试套件是以下文档的 **Test Anchor**：
> [**Bootstrap Compute SSOT**](../../../../docs/ssot/bootstrap.compute.md#5-验证与测试-the-proof)

## 测试矩阵

| 组件 | 测试 | 标记 | 验证内容 |
|------|------|------|----------|
| **K3s** | `test_k3s_api_accessible` | smoke | API 可达性 |
| **K3s** | `test_k3s_namespaces_exist` | - | Namespace 结构 |
| **K3s** | `test_k3s_core_services_running` | - | 核心服务运行 |
| **Digger** | `test_digger_endpoint_accessible` | - | Webhook 端点可达 |
| **Traefik** | `test_traefik_routes_traffic` | smoke | 路由功能 |
| **Traefik** | `test_traefik_https_redirect` | - | HTTPS 重定向 |
| **Traefik** | `test_traefik_preserves_headers` | - | Header 保留 |
| **Traefik** | `test_traefik_handles_invalid_routes` | - | 无效路由处理 |

## 运行测试

```bash
# 所有计算层测试
uv run pytest tests/bootstrap/compute/ -v

# Smoke 测试
uv run pytest tests/bootstrap/compute/ -m smoke -v
```