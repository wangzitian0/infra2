# Dokploy 安装

初始化 VPS 时安装 Dokploy。

## 操作步骤

```bash
# 1. SSH 登录 VPS
ssh root@<VPS_IP>

# 2. 执行安装脚本
curl -sSL https://dokploy.com/install.sh | sh

# 3. 验证安装
docker ps | grep dokploy
curl -I http://localhost:3000
```

## 后续步骤

- 访问 `http://<VPS_IP>:3000` 完成初始配置（创建账户）
- 更新 [SSOT 版本追踪表](../../docs/ssot/bootstrap.nodep.md#4-版本追踪)

## 相关资源

- [Dokploy 官方文档](https://docs.dokploy.com)
- [安装脚本](https://dokploy.com/install.sh)
