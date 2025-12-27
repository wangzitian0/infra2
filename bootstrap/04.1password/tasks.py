"""
1Password Connect 部署自动化任务
"""
import os
from invoke import task


# Environment variables
VPS_HOST = os.getenv("VPS_HOST", "${VPS_HOST}")
INTERNAL_DOMAIN = os.getenv("INTERNAL_DOMAIN", "${INTERNAL_DOMAIN}")


@task
def prepare(c):
    """准备 1Password 数据目录"""
    print("\n📁 准备 1Password 数据目录...")
    
    # 创建目录
    c.run(f"ssh root@{VPS_HOST} 'mkdir -p /data/bootstrap/1password'")
    
    # 设置权限（777 允许容器写入数据库文件）
    c.run(f"ssh root@{VPS_HOST} 'chown -R 1000:1000 /data/bootstrap/1password'")
    c.run(f"ssh root@{VPS_HOST} 'chmod 777 /data/bootstrap/1password'")
    
    # 验证
    result = c.run(f"ssh root@{VPS_HOST} 'ls -la /data/bootstrap/1password'", hide=True)
    print(result.stdout)
    print("✅ 目录准备完成")


@task
def upload_credentials(c):
    """上传 1Password credentials 文件"""
    print("\n📤 上传 credentials 文件...")
    
    # 使用 1Password CLI 读取并上传
    print("从 1Password Vault 读取 credentials...")
    cmd = f"op document get 'VPS-01 Credentials File' --vault Infra2 | ssh root@{VPS_HOST} 'cat > /data/bootstrap/1password/1password-credentials.json && chown 1000:1000 /data/bootstrap/1password/1password-credentials.json'"
    
    result = c.run(cmd, warn=True)
    if not result.ok:
        print("❌ 上传失败，请确保：")
        print("  1. 已安装 1Password CLI (op)")
        print("  2. 已登录: eval $(op signin)")
        print("  3. Vault 'Infra2' 中存在 'VPS-01 Credentials File'")
        raise Exception("Credentials 上传失败")
    
    # 验证上传
    result = c.run(f"ssh root@{VPS_HOST} 'ls -lh /data/bootstrap/1password/1password-credentials.json'")
    print("✅ Credentials 已上传")


@task(pre=[prepare, upload_credentials])
def deploy(c):
    """部署 1Password Connect 到 Dokploy"""
    print("\n🚀 部署 1Password Connect...")
    print("\n" + "="*60)
    print("⏸️  请在 Dokploy UI 完成以下操作:")
    print("="*60)
    print(f"1. 访问: https://cloud.{INTERNAL_DOMAIN}")
    print("2. 创建 Project: bootstrap (如果不存在)")
    print("3. 创建 Docker Compose 应用:")
    print("   - Name: 1password-connect")
    print("   - Repository: GitHub → wangzitian0/infra2")
    print("   - Branch: main")
    print("   - Compose Path: bootstrap/04.1password/compose.yaml")
    print("4. 点击 Deploy")
    print("5. 等待部署完成（观察日志）")
    print("="*60)
    
    input("\n✋ 完成上述步骤后，按 Enter 继续...")
    
    # 验证部署
    print("\n🔍 验证 1Password Connect 服务...")
    result = c.run(f"curl -s https://op.{INTERNAL_DOMAIN}/health", warn=True)
    if result.ok and "1Password Connect" in result.stdout:
        print("✅ 1Password Connect 服务正常")
        print(result.stdout)
    else:
        print("⚠️  服务暂时无法访问（可能需要等待几分钟）")


@task(pre=[deploy])
def verify(c):
    """验证 1Password Connect 功能"""
    print("\n🔍 验证 1Password Connect...")
    
    # 健康检查
    print("1. 健康检查:")
    result = c.run(f"curl -s https://op.{INTERNAL_DOMAIN}/health", warn=True)
    if result.ok:
        print(result.stdout)
    
    # 测试读取 secrets（可选）
    print("\n2. 测试读取 secrets（需要 Access Token）:")
    print("   执行以下命令测试:")
    print(f"   TOKEN=$(op item get 'VPS-01 Access Token: own_service' --vault Infra2 --fields credential --reveal)")
    print(f"   curl -H \"Authorization: Bearer $TOKEN\" https://op.{INTERNAL_DOMAIN}/v1/vaults")


@task
def status(c):
    """检查 1Password Connect 状态"""
    print(f"\n🔍 检查 1Password Connect 状态...")
    
    # 检查 HTTP
    c.run(f"curl -s https://op.{INTERNAL_DOMAIN}/health", warn=True)
    
    # 检查容器
    print(f"\n检查容器状态:")
    c.run(f"ssh root@{VPS_HOST} 'docker ps | grep op-connect'", warn=True)
    
    # 检查数据目录
    print(f"\n检查数据目录:")
    c.run(f"ssh root@{VPS_HOST} 'ls -lh /data/bootstrap/1password/'", warn=True)


@task
def fix_permissions(c):
    """修复数据库权限问题"""
    print("\n🔧 修复权限问题...")
    c.run(f"ssh root@{VPS_HOST} 'chmod 777 /data/bootstrap/1password'")
    print("✅ 权限已修复为 777")
    print("建议在 Dokploy 中重新部署应用")


@task(pre=[prepare, upload_credentials, deploy, verify])
def setup(c):
    """完整的 1Password Connect 设置流程"""
    print("\n✅ 1Password Connect 设置完成！")
    print(f"\n访问地址: https://op.{INTERNAL_DOMAIN}")
    print("\n记得更新 SSOT 版本追踪表:")
    print("docs/ssot/bootstrap.nodep.md")
