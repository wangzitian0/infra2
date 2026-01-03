# Appwrite

Appwrite 是一个开源的后端即服务 (BaaS) 平台，提供 5 个核心模块：

1. **Auth** - 用户认证和会话管理
2. **Databases** - 文档数据库
3. **Storage** - 文件存储（使用 MinIO 作为 S3 后端）
4. **Functions** - 无服务器函数
5. **Messaging** - 消息推送

## 配置文件

- `compose.yml` - Docker Compose 配置
- `.env` - 环境变量（已加入 .gitignore）
- `minio-policy.json` - MinIO bucket 访问策略

## 脚本

- `scripts/e2e-test.sh` - 端到端测试脚本，测试所有 5 个核心模块
- `scripts/fix-bucket-permissions.sql` - 修复 bucket 权限问题的 SQL 脚本

## 运行 E2E 测试

```bash
# 复制脚本到容器并运行
docker cp scripts/e2e-test.sh appwrite:/tmp/
docker exec appwrite sh /tmp/e2e-test.sh
```

## 常见问题

### Storage 上传失败 - "No permissions provided for action 'create'"

这通常是因为 bucket collection 的权限表未正确初始化。运行修复脚本：

```bash
# 查看当前权限状态
docker exec appwrite-mariadb mysql -uuser -p<password> appwrite -e "
  SELECT * FROM _1_buckets_perms;
  SELECT _uid, _permissions FROM _1__metadata WHERE _uid = 'bucket_1';
"

# 应用修复
docker exec appwrite-mariadb mysql -uuser -p<password> appwrite < scripts/fix-bucket-permissions.sql

# 清除 Redis 缓存
docker exec appwrite-redis redis-cli FLUSHALL
```

### S3/MinIO 连接问题

确保 `_APP_STORAGE_S3_ENDPOINT` 使用内部 Docker 网络地址：

```
_APP_STORAGE_S3_ENDPOINT=http://platform-minio:9000
```

而不是外部 CDN 地址（如 `https://s3.example.com`）。

## 版本

- Appwrite: 1.8.1
- Console: 7.5.7
- Executor: 0.7.22
