# Bootstrap 操作手册

> **定位**：Bootstrap 层操作指南，包含 Terraform 和非 Terraform 管理的组件安装步骤。
> **SSOT 参考**：[docs/ssot/bootstrap.nodep.md](../docs/ssot/bootstrap.nodep.md)

---

## 非 Terraform 管理组件

### Dokploy 安装

> **官方文档**：[docs.dokploy.com](https://docs.dokploy.com)
> **GitHub**：[dokploy/dokploy](https://github.com/dokploy/dokploy)

**1. SSH 登录 VPS**

```bash
ssh root@<VPS_IP>
```

**2. 执行安装脚本**

```bash
curl -sSL https://dokploy.com/install.sh | sh
```

**3. 验证安装**

```bash
# 检查服务状态
docker ps | grep dokploy

# 访问 Web UI
curl -I http://localhost:3000
```

**4. 访问管理界面**

浏览器访问 `http://<VPS_IP>:3000`，完成初始配置。

**5. 更新 SSOT 版本记录**

安装完成后，更新 [bootstrap.nodep.md](../docs/ssot/bootstrap.nodep.md) 中的版本追踪表。

---

## 相关文档

- **SSOT**：[bootstrap.nodep.md](../docs/ssot/bootstrap.nodep.md) - 非 Terraform 组件定义
