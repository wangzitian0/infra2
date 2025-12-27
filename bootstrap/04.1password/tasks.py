"""
1Password Connect 部署自动化任务
"""
import os
from invoke import task


# Environment variables (Lazy loaded to allow check_env to run first)
def get_vps_host():
    return os.environ.get("VPS_HOST")

def get_internal_domain():
    return os.environ.get("INTERNAL_DOMAIN")


@task
def check_env(c):
    """验证必要的环境变量是否存在"""
    missing = []
    if not get_vps_host():
        missing.append("VPS_HOST")
    if not get_internal_domain():
        missing.append("INTERNAL_DOMAIN")
    
    if missing:
        print("\n❌ 错误: 缺少必要的环境变量!")
        print(f"请在 .env 文件中设置: {', '.join(missing)}")
        print("或者执行: export VPS_HOST=xxx INTERNAL_DOMAIN=xxx")
        exit(1)
    print("✅ 环境变量验证通过")


@task(pre=[check_env])
def prepare(c):
    """准备 1Password 数据目录"""
    vps_host = get_vps_host()
    print("\n📁 准备 1Password 数据目录...")
    
    # 创建目录
    c.run(f"ssh root@{vps_host} 'mkdir -p /data/bootstrap/1password'")
    
    # 设置权限（777 允许容器写入数据库文件）
    c.run(f"ssh root@{vps_host} 'chown -R 1000:1000 /data/bootstrap/1password'")
    c.run(f"ssh root@{vps_host} 'chmod 777 /data/bootstrap/1password'")
    
    # 验证
    result = c.run(f"ssh root@{vps_host} 'ls -la /data/bootstrap/1password'", hide=True)
    print(result.stdout)
    print("✅ 目录准备完成")


@task(pre=[check_env])
def upload_credentials(c):
    """上传 1Password credentials 文件"""
    vps_host = get_vps_host()
    print("\n📤 上传 credentials 文件...")
    
    # 使用 1Password CLI 读取并上传
    print("从 1Password Vault 读取 credentials...")
    cmd = f"op document get 'bootstrap-1password-VPS-01 Credentials File' --vault Infra2 | ssh root@{vps_host} 'cat > /data/bootstrap/1password/1password-credentials.json && chown 1000:1000 /data/bootstrap/1password/1password-credentials.json'"
    
    result = c.run(cmd, warn=True)
    if not result.ok:
        print("❌ 上传失败，请确保：")
        print("  1. 已安装 1Password CLI (op)")
        print("  2. 已登录: eval $(op signin)")
        print("  3. Vault 'Infra2' 中存在 'VPS-01 Credentials File'")
        raise Exception("Credentials 上传失败")
    
    # 验证上传
    result = c.run(f"ssh root@{vps_host} 'ls -lh /data/bootstrap/1password/1password-credentials.json'")
    print("✅ Credentials 已上传")


@task(pre=[check_env, prepare, upload_credentials])
def deploy(c):
    """部署 1Password Connect 到 Dokploy"""
    internal_domain = get_internal_domain()
    print("\n🚀 部署 1Password Connect...")
    print("\n" + "="*60)
    print("⏸️  请在 Dokploy UI 完成以下操作:")
    print("="*60)
    print(f"1. 访问: https://cloud.{internal_domain}")
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
    result = c.run(f"curl -s https://op.{internal_domain}/health", warn=True)
    if result.ok and "1Password Connect" in result.stdout:
        print("✅ 1Password Connect 服务正常")
        print(result.stdout)
    else:
        print("⚠️  服务暂时无法访问（可能需要等待几分钟）")


@task(pre=[check_env, deploy])
def verify(c):
    """验证 1Password Connect 功能"""
    internal_domain = get_internal_domain()
    print("\n🔍 验证 1Password Connect...")
    
    # 健康检查
    print("1. 健康检查:")
    result = c.run(f"curl -s https://op.{internal_domain}/health", warn=True)
    if result.ok:
        print(result.stdout)
    
    # 测试读取 secrets（可选）
    print("\n2. 测试读取 secrets（需要 Access Token）:")
    print("   执行以下命令测试:")
    print(f"   TOKEN=$(op item get 'VPS-01 Access Token: own_service' --vault Infra2 --fields credential --reveal)")
    print(f"   curl -H \"Authorization: Bearer $TOKEN\" https://op.{internal_domain}/v1/vaults")


@task(pre=[check_env])
def status(c):
    """检查 1Password Connect 状态"""
    internal_domain = get_internal_domain()
    vps_host = get_vps_host()
    print(f"\n🔍 检查 1Password Connect 状态...")
    
    # 检查 HTTP
    c.run(f"curl -s https://op.{internal_domain}/health", warn=True)
    
    # 检查容器
    print(f"\n检查容器状态:")
    c.run(f"ssh root@{vps_host} 'docker ps | grep op-connect'", warn=True)
    
    # 检查数据目录
    print(f"\n检查数据目录:")
    c.run(f"ssh root@{vps_host} 'ls -lh /data/bootstrap/1password/'", warn=True)


@task(pre=[check_env])
def fix_permissions(c):
    """修复数据库权限问题"""
    vps_host = get_vps_host()
    print("\n🔧 修复权限问题...")
    c.run(f"ssh root@{vps_host} 'chmod 777 /data/bootstrap/1password'")
    print("✅ 权限已修复为 777")
    print("建议在 Dokploy 中重新部署应用")


@task(pre=[check_env, prepare, upload_credentials, deploy, verify])
def setup(c):
    """完整的 1Password Connect 设置流程"""
    internal_domain = get_internal_domain()
    print("\n✅ 1Password Connect 设置完成！")
    print(f"\n访问地址: https://op.{internal_domain}")
    print("\n记得更新 SSOT 版本追踪表:")
    print("docs/ssot/bootstrap.nodep.md")
