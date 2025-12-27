# Bootstrap

> **定位**：引导层，包含系统启动所需的基础组件安装
> **SSOT 参考**：[docs/ssot/bootstrap.nodep.md](../docs/ssot/bootstrap.nodep.md)

---

## 📁 目录结构

```
./
└── README.md         # 本文件（操作手册）
```

---

## 🔧 如何修改本目录

### 修改前必读

1. **阅读 SSOT**：先查阅 [bootstrap.nodep.md](../docs/ssot/bootstrap.nodep.md) 了解设计约束
2. **确认影响范围**：Bootstrap 是 Trust Anchor，变更需谨慎
3. **检查依赖**：上层 Platform/Data 依赖本层

### 常见修改场景

| 场景 | 操作步骤 | 注意事项 |
|------|----------|----------|
| **添加新组件** | 1. 安装 → 2. 更新本 README → 3. 更新 SSOT | 记录版本信息 |
| **升级组件** | 1. 执行升级 → 2. 更新 SSOT 版本表 | 备份数据 |
| **删除组件** | 1. 确认无依赖 → 2. 卸载 → 3. 更新 README 和 SSOT | ⚠️ 谨慎操作 |

---

## 📖 操作指南

### Dokploy 安装

**触发条件**：新 VPS 初始化

```bash
# 1. SSH 登录 VPS
ssh root@<VPS_IP>

# 2. 执行安装脚本
curl -sSL https://dokploy.com/install.sh | sh

# 3. 验证安装
docker ps | grep dokploy
curl -I http://localhost:3000
```

**后续步骤**：
- 访问 `http://<VPS_IP>:3000` 完成初始配置
- 更新 [SSOT 版本追踪表](../docs/ssot/bootstrap.nodep.md#4-版本追踪)

---

## 相关文档

- **SSOT**：[bootstrap.nodep.md](../docs/ssot/bootstrap.nodep.md) - 非 TF 组件定义
