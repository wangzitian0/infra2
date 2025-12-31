#!/bin/bash
# Dokploy v0.25.11 安装脚本
# 用法: ./install_dokploy.sh

set -e

# 配置
OP_VAULT="Infra2"
# 1Password Item: "init/env_vars" (使用 ID 以确保唯一性)
OP_ITEM_ID="haih7qcpar5o2hxwllrpua7f2e"

echo "=== Dokploy v0.25.11 安装脚本 ==="
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
read -p "📌 即将在 VPS 上安装 Dokploy v0.25.11，继续？ (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "🚫 已取消"
    exit 0
fi

echo ""
echo "🚀 开始安装 Dokploy v0.25.11..."
echo ""

# SSH 到 VPS 并安装
ssh root@"${VPS_IP}" << 'ENDSSH'
set -e

echo "📦 下载并执行 Dokploy 安装脚本 (v0.25.11)..."
curl -sSL https://dokploy.com/install.sh | DOKPLOY_VERSION=v0.25.11 sh

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
echo "🎉 Dokploy v0.25.11 安装成功！"
echo ""
echo "📝 下一步："
echo "1. 访问 http://${VPS_IP}:3000 创建管理员账户"
echo "2. 运行: invoke dns_and_cert.setup"
echo "3. 配置 Dokploy 域名: https://cloud.${INTERNAL_DOMAIN}"
echo ""
