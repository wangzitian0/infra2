"""
Vault 部署自动化任务
"""
import os
from invoke import task


# Environment variables
VPS_HOST = os.getenv("VPS_HOST", "${VPS_HOST}")
INTERNAL_DOMAIN = os.getenv("INTERNAL_DOMAIN", "${INTERNAL_DOMAIN}")


@task
def prepare(c):
    """准备 Vault 数据目录"""
    print("\n📁 准备 Vault 数据目录...")
    
    # 创建目录
    c.run(f"ssh root@{VPS_HOST} 'mkdir -p /data/bootstrap/vault/{{file,logs,config}}'")
    
    # 设置权限
    c.run(f"ssh root@{VPS_HOST} 'chown -R 1000:1000 /data/bootstrap/vault'")
    c.run(f"ssh root@{VPS_HOST} 'chmod 755 /data/bootstrap/vault'")
    
    # 验证
    result = c.run(f"ssh root@{VPS_HOST} 'ls -la /data/bootstrap/vault'", hide=True)
    print(result.stdout)
    print("✅ 目录准备完成")


@task
def upload_config(c):
    """上传 Vault 配置文件"""
    print("\n📤 上传 Vault 配置文件...")
    
    config_file = "bootstrap/05.vault/vault.hcl"
    if not os.path.exists(config_file):
        raise Exception(f"❌ 配置文件不存在: {config_file}")
    
    c.run(f"scp {config_file} root@{VPS_HOST}:/data/bootstrap/vault/config/")
    
    # 验证上传
    result = c.run(f"ssh root@{VPS_HOST} 'cat /data/bootstrap/vault/config/vault.hcl'", hide=True)
    print("✅ 配置文件已上传:")
    print(result.stdout[:200] + "..." if len(result.stdout) > 200 else result.stdout)


@task(pre=[prepare, upload_config])
def deploy(c):
    """部署 Vault 到 Dokploy"""
    print("\n🚀 部署 Vault...")
    print("\n" + "="*60)
    print("⏸️  请在 Dokploy UI 完成以下操作:")
    print("="*60)
    print(f"1. 访问: https://cloud.{INTERNAL_DOMAIN}")
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
    result = c.run(f"curl -I https://vault.{INTERNAL_DOMAIN}", warn=True, hide=True)
    if result.ok:
        print("✅ Vault 服务可访问")
    else:
        print("⚠️  Vault 服务暂时无法访问（可能需要等待几分钟）")


@task(pre=[deploy])
def init(c):
    """初始化 Vault"""
    print("\n🔐 初始化 Vault...")
    print("\n" + "="*60)
    print("⚠️  重要：请妥善保存以下信息！")
    print("="*60)
    
    # 设置 VAULT_ADDR
    os.environ["VAULT_ADDR"] = f"https://vault.{INTERNAL_DOMAIN}"
    
    print(f"\n执行: vault operator init")
    print("(请手动执行以下命令)")
    print(f"export VAULT_ADDR=https://vault.{INTERNAL_DOMAIN}")
    print("vault operator init")
    
    input("\n✋ 完成初始化后，按 Enter 继续...")
    
    print("\n📋 后续步骤:")
    print("1. 保存 5 个 unseal keys 到 1Password")
    print("2. 保存 root token 到 1Password")
    print("3. 每次重启后需要 unseal (至少 3 个 keys)")
    print("4. 配置审计日志: vault audit enable file file_path=/vault/logs/audit.log")


@task
def status(c):
    """检查 Vault 状态"""
    print(f"\n🔍 检查 Vault 状态...")
    
    # 检查 HTTP
    result = c.run(f"curl -s https://vault.{INTERNAL_DOMAIN}/v1/sys/health || echo 'Failed'", warn=True)
    
    # 检查容器
    print(f"\n检查容器状态:")
    c.run(f"ssh root@{VPS_HOST} 'docker ps | grep vault'", warn=True)


@task(pre=[prepare, upload_config, deploy, init])
def setup(c):
    """完整的 Vault 设置流程"""
    print("\n✅ Vault 设置完成！")
    print(f"\n访问地址: https://vault.{INTERNAL_DOMAIN}")
    print("\n记得更新 SSOT 版本追踪表:")
    print("docs/ssot/bootstrap.nodep.md")
