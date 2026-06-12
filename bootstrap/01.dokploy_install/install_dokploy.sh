#!/bin/bash
# Dokploy v0.29.8 安装脚本
# 用法: ./install_dokploy.sh
#
# 注意（全新安装 vs 升级）:
#   - 本脚本是【全新安装】(curl install.sh)，v0.29.8 直接装即可。
#   - 若要【升级】一台已在运行的主机 (v0.25.11 -> v0.29.8)，不要用 --update-order
#     滚动 `docker service update`：dokploy 是 host-mode 端口 (3000) 的 swarm
#     service，连发 update 会把端口预留状态卡死、新 task 一直 "Preparing"。
#     正确姿势：
#       1) 先 `docker pull dokploy/dokploy:v0.29.8` 把镜像完整拉到本地(~3GB，
#          否则 scale 切换时仍在后台拉镜像，被中途打断就一直 Preparing)。
#       2) `docker service scale dokploy=0` -> 等 ≥30s 让 swarm 释放端口预留
#          -> `docker service update --image dokploy/dokploy:v0.29.8 dokploy`
#          -> `docker service scale dokploy=1`。
#   - v0.29.8 修复了 v0.25.11 的 compose.delete 不删容器(预览环境容器泄露)，
#     以及 host 级 schedule 必须用 `dokploy-server` 类型才会真正执行。

set -e

# 配置
OP_VAULT="Infra2"
# 1Password Item: "init/env_vars" (使用 ID 以确保唯一性)
OP_ITEM_ID="haih7qcpar5o2hxwllrpua7f2e"

echo "=== Dokploy v0.29.8 安装脚本 ==="
echo ""

# 获取 VPS IP
echo "📋 从 1Password 获取 VPS 信息..."
VPS_IP=$(op item get "$OP_ITEM_ID" --vault "$OP_VAULT" --fields VPS_HOST --format json | jq -r '.value')
INTERNAL_DOMAIN=$(op item get "$OP_ITEM_ID" --vault "$OP_VAULT" --fields INTERNAL_DOMAIN --format json | jq -r '.value')

if [ -z "$VPS_IP" ]; then
    echo "❌ 无法获取 VPS_HOST"
    exit 1
fi

# 安全检查: 验证 VPS_IP 格式 (防止注入)
if [[ ! "$VPS_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && [[ ! "$VPS_IP" =~ ^[a-zA-Z0-9.-]+$ ]]; then
    echo "❌ 错误: VPS_HOST 格式无效或包含非法字符"
    exit 1
fi

echo "✓ VPS IP: $VPS_IP"
echo "✓ Internal Domain: $INTERNAL_DOMAIN"
echo ""

# 确认继续
read -p "📌 即将在 VPS 上安装 Dokploy v0.29.8，继续？ (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "🚫 已取消"
    exit 0
fi

echo ""
echo "🚀 开始安装 Dokploy v0.29.8..."
echo ""

# SSH 到 VPS 并安装
ssh root@"${VPS_IP}" << 'ENDSSH'
set -e

echo "📦 下载并执行 Dokploy 安装脚本 (v0.29.8)..."
curl -sSL https://dokploy.com/install.sh | DOKPLOY_VERSION=v0.29.8 sh

echo ""
echo "✅ Dokploy 安装完成"
echo ""
echo "📊 验证安装..."
docker ps | grep dokploy

echo ""
echo "🌐 测试 HTTP 访问..."
curl -I http://localhost:3000 2>&1 | head -1

echo ""
echo "✅ 验证完成！"
ENDSSH

echo ""
echo "🎉 Dokploy v0.29.8 安装成功！"
echo ""
echo "📝 下一步："
echo "1. 访问 http://${VPS_IP}:3000 创建管理员账户"
echo "2. 运行: invoke dns_and_cert.setup"
echo "3. 配置 Dokploy 域名: https://cloud.${INTERNAL_DOMAIN}"
echo ""
