# Dokploy 安装

初始化 VPS 时安装 Dokploy。

## 操作步骤

```bash
# 1. SSH 登录 VPS
ssh root@<VPS_IP>

# 2. 执行安装脚本（指定版本）
# 推荐使用 v0.29.8（修复 v0.25.11 的预览容器泄露 + dokploy-server schedule 执行；
# v0.26.x 曾有部署问题，v0.29.8 已验证可用）
curl -sSL https://dokploy.com/install.sh | DOKPLOY_VERSION=v0.29.8 sh

# 如果需要安装最新版本（风险自负）
# curl -sSL https://dokploy.com/install.sh | sh

# 3. 验证安装
docker ps | grep dokploy
curl -I http://localhost:3000
```

## 后续步骤

- 完成初始配置（创建账户）：主机防火墙不对公网开放 3000，用 SSH 隧道 `ssh -L 3000:localhost:3000 root@<VPS_IP>` 后访问 `http://localhost:3000`；配置好域名后改用 `https://cloud.<DOMAIN>`
- 安装主机防火墙（见下节）
- 更新 [SSOT 版本追踪表](../../docs/ssot/bootstrap.nodep.md#4-版本追踪)

## 主机防火墙（`hostfw/`）

公网只放行 SSH 22 和 Traefik 80/443（含 443/udp）；Dokploy UI 3000、swarm 2377/7946/4789 不对公网开放。设计与约束见 [SSOT §主机防火墙](../../docs/ssot/bootstrap.nodep.md#host-firewall)。

```bash
# 在 VPS 上，从本目录的副本执行（root）
scp -r bootstrap/01.dokploy_install/hostfw root@<VPS_IP>:/root/infra2-hostfw
ssh root@<VPS_IP>
cd /root/infra2-hostfw
bash hostfw.sh check     # 语法检查 + 确认公网网卡
bash hostfw.sh apply     # 先挂 5 分钟自动回滚，再加载规则
# 另开一个新的 SSH 登录，确认能进
bash hostfw.sh confirm   # 取消回滚
bash hostfw.sh install   # 写入 /etc/infra2/hostfw.nft，启用 infra2-hostfw.service
bash hostfw.sh status    # 丢弃计数、回滚状态、已安装规则与本副本是否一致
```

| 文件 | 作用 |
|------|------|
| `hostfw.nft` | 规则集，只管 `table inet infra2_hostfw`，不 flush 其他表 |
| `infra2-hostfw.service` | 开机加载 `/etc/infra2/hostfw.nft` |
| `hostfw.sh` | check / apply / confirm / install / status |

## 相关资源

- [Dokploy 官方文档](https://docs.dokploy.com)
- [安装脚本](https://dokploy.com/install.sh)
