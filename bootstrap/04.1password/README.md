# 1Password Connect

自托管密钥管理服务。

##  配置文件

- [`compose.yaml`](./compose.yaml) - Docker Compose 配置

## 操作步骤

### 1. 获取 Credentials

前往 https://my.1password.com/integrations/
创建 1Password Connect Server，下载 `1password-credentials.json`

### 2. 准备数据目录

```bash
ssh ${VPS_SSH_USER:-root}@<VPS_IP>
mkdir -p /data/bootstrap/1password
chown -R 1000:1000 /data/bootstrap/1password
chmod 750 /data/bootstrap/1password
exit
```

### 3. 上传 Credentials

```bash
op document get "bootstrap/1password/VPS-01 Credentials File" --vault Infra2 | \
  ssh ${VPS_SSH_USER:-root}@<VPS_IP> 'cat > /data/bootstrap/1password/1password-credentials.json && chown 1000:1000 /data/bootstrap/1password/1password-credentials.json'

# 验证
ssh ${VPS_SSH_USER:-root}@<VPS_IP> 'ls -la /data/bootstrap/1password/'
```

### 4. 在 Dokploy 部署

- 访问 https://cloud.$INTERNAL_DOMAIN
- 创建 Project: bootstrap
- 创建 Docker Compose 应用: 1password-connect
- Repository: GitHub → wangzitian0/infra2
- Branch: main
- Compose Path: `bootstrap/04.1password/compose.yaml`

### 5. 验证部署

```bash
curl https://op.$INTERNAL_DOMAIN/health
# 预期响应: {"name":"1Password Connect API","version":"1.8.1",...}

# 测试读取 secrets
TOKEN=$(op item get "bootstrap/1password/VPS-01 Access Token: own_service" --vault Infra2 --fields credential --reveal)
curl -H "Authorization: Bearer $TOKEN" https://op.$INTERNAL_DOMAIN/v1/vaults
```

## 常见问题

### 数据库权限错误
```bash
ssh root@<VPS_IP> 'chmod 750 /data/bootstrap/1password'
```

### sync 服务无法启动
- 确认目录权限是 `777`
- 检查 credentials 文件是否存在

### API 返回 404
- 等待 1-2 分钟让服务完全启动
- 检查容器状态：`docker ps | grep op-connect`
