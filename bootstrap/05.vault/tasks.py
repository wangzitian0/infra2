"""
Vault 部署自动化任务
"""
import os
from invoke import task


# Environment variables
VPS_HOST = os.environ.get("VPS_HOST")
INTERNAL_DOMAIN = os.environ.get("INTERNAL_DOMAIN")


@task
def prepare(c):
    """准备 Vault 数据目录"""
    print("\n📁 准备 Vault 数据目录...")
    c.run(f"ssh root@{VPS_HOST} 'mkdir -p /data/bootstrap/vault/{{file,logs,config}}'")
    c.run(f"ssh root@{VPS_HOST} 'chown -R 1000:1000 /data/bootstrap/vault'")
    c.run(f"ssh root@{VPS_HOST} 'chmod 755 /data/bootstrap/vault'")
    print("✅ 目录准备完成")


@task
def upload_config(c):
    """上传 Vault 配置文件"""
    print("\n📤 上传 Vault 配置文件...")
    config_file = "bootstrap/05.vault/vault.hcl"
    c.run(f"scp {config_file} root@{VPS_HOST}:/data/bootstrap/vault/config/")
    print("✅ 配置文件已上传")


@task(pre=[prepare, upload_config])
def deploy(c):
    """部署 Vault 到 Dokploy (手动步骤提示)"""
    print("\n🚀 部署 Vault...")
    print(f"请在 Dokploy 中使用分支或合入主干，并确保 OP_CONNECT_TOKEN 已配置。")
    print(f"访问地址: https://cloud.{INTERNAL_DOMAIN}")
    input("\n✋ 完成操作后，按 Enter 继续...")


@task(pre=[deploy])
def init(c):
    """初始化 Vault"""
    print("\n🔐 初始化 Vault...")
    print(f"export VAULT_ADDR=https://vault.{INTERNAL_DOMAIN}")
    print("vault operator init")
    input("\n✋ 完成初始化并将 Key 存入 1Password 后，按 Enter 继续...")


@task
def unseal(c):
    """(手动触发) 命令哨兵容器立即执行一次解封检查"""
    print("\n🔐 正在通知哨兵容器执行解封检查...")
    c.run(f"ssh root@{VPS_HOST} 'docker logs --tail 20 vault-unsealer'", warn=True)
    c.run(f"ssh root@{VPS_HOST} 'docker restart vault-unsealer'")
    print("✅ 哨兵已重启并触发首轮检查，请观察上述日志。")


@task
def status(c):
    """检查 Vault 状态"""
    print(f"\n🔍 检查 Vault 状态...")
    c.run(f"curl -s https://vault.{INTERNAL_DOMAIN}/v1/sys/health", warn=True)
    c.run(f"ssh root@{VPS_HOST} 'docker ps | grep vault'", warn=True)


@task(pre=[prepare, upload_config, deploy, init, unseal])
def setup(c):
    """完整的 Vault 设置流程 (包含自动解封)"""
    print("\n✅ Vault 设置完成！哨兵容器将处理后续解封。")
    print(f"\n访问地址: https://vault.{INTERNAL_DOMAIN}")
