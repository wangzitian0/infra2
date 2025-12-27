"""
Vault 部署自动化任务
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
    """准备 Vault 数据目录"""
    vps_host = get_vps_host()
    print("\n📁 准备 Vault 数据目录...")
    
    # 创建目录
    c.run(f"ssh root@{vps_host} 'mkdir -p /data/bootstrap/vault/{{file,logs,config}}'")
    
    # 设置权限
    c.run(f"ssh root@{vps_host} 'chown -R 1000:1000 /data/bootstrap/vault'")
    c.run(f"ssh root@{vps_host} 'chmod 755 /data/bootstrap/vault'")
    
    # 验证
    result = c.run(f"ssh root@{vps_host} 'ls -la /data/bootstrap/vault'", hide=True)
    print(result.stdout)
    print("✅ 目录准备完成")


@task(pre=[check_env])
def upload_config(c):
    """上传 Vault 配置文件"""
    vps_host = get_vps_host()
    print("\n📤 上传 Vault 配置文件...")
    
    config_file = "bootstrap/05.vault/vault.hcl"
    if not os.path.exists(config_file):
        raise Exception(f"❌ 配置文件不存在: {config_file}")
    
    c.run(f"scp {config_file} root@{vps_host}:/data/bootstrap/vault/config/")
    
    # 验证上传
    result = c.run(f"ssh root@{vps_host} 'cat /data/bootstrap/vault/config/vault.hcl'", hide=True)
    print("✅ 配置文件已上传:")
    print(result.stdout[:200] + "..." if len(result.stdout) > 200 else result.stdout)


@task(pre=[check_env, prepare, upload_config])
def deploy(c):
    """部署 Vault 到 Dokploy"""
    internal_domain = get_internal_domain()
    print("\n🚀 部署 Vault...")
    print("\n" + "="*60)
    print("⏸️  请在 Dokploy UI 完成以下操作:")
    print("="*60)
    print(f"1. 访问: https://cloud.{internal_domain}")
    print("2. 创建 Project: bootstrap (如果不存在)")
    print("3. 创建 Docker Compose 应用:")
    print("   - Name: vault")
    print("   - Repository: GitHub → wangzitian0/infra2")
    print("   - Branch: main")
    print("   - Compose Path: bootstrap/05.vault/compose.yaml")
    print("4. 点击 Deploy")
    print("5. 等待部署完成（观察日志）")
    print("="*60)
    
    input("\n✋ 完成上述步骤后，按 Enter 继续...")
    
    # 验证部署
    print("\n🔍 验证 Vault 服务...")
    result = c.run(f"curl -I https://vault.{internal_domain}", warn=True, hide=True)
    if result.ok:
        print("✅ Vault 服务可访问")
    else:
        print("⚠️  Vault 服务暂时无法访问（可能需要等待几分钟）")


@task(pre=[check_env, deploy])
def init(c):
    """初始化 Vault"""
    internal_domain = get_internal_domain()
    print("\n🔐 初始化 Vault...")
    print("\n" + "="*60)
    print("⚠️  重要：请妥善保存以下信息！")
    print("="*60)
    
    # 设置 VAULT_ADDR
    os.environ["VAULT_ADDR"] = f"https://vault.{internal_domain}"
    
    print(f"\n执行: vault operator init")
    print("(请手动执行以下命令)")
    print(f"export VAULT_ADDR=https://vault.{internal_domain}")
    print("vault operator init")
    
    input("\n✋ 完成初始化后，按 Enter 继续...")
    
    print("\n📋 后续步骤:")
    print("1. 保存 5 个 unseal keys 到 1Password")
    print("2. 保存 root token 到 1Password")
    print("3. 每次重启后需要 unseal (至少 3 个 keys)")
    print("4. 配置审计日志: vault audit enable file file_path=/vault/logs/audit.log")


@task(pre=[check_env])
def status(c):
    """检查 Vault 状态"""
    internal_domain = get_internal_domain()
    vps_host = get_vps_host()
    print(f"\n🔍 检查 Vault 状态...")
    
    # 检查 HTTP
    result = c.run(f"curl -s https://vault.{internal_domain}/v1/sys/health || echo 'Failed'", warn=True)
    
    # 检查容器
    print(f"\n检查容器状态:")
    c.run(f"ssh root@{vps_host} 'docker ps | grep vault'", warn=True)


@task(pre=[check_env, prepare, upload_config, deploy, init])
def setup(c):
    """完整的 Vault 设置流程"""
    internal_domain = get_internal_domain()
    print("\n✅ Vault 设置完成！")
    print(f"\n访问地址: https://vault.{internal_domain}")
    print("\n记得更新 SSOT 版本追踪表:")
    print("docs/ssot/bootstrap.nodep.md")
