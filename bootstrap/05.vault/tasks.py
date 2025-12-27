"""
Vault deployment automation tasks
"""
import os
from invoke import task


# Environment variables
VPS_HOST = os.environ.get("VPS_HOST")
INTERNAL_DOMAIN = os.environ.get("INTERNAL_DOMAIN")


@task
def prepare(c):
    """Prepare Vault data directory"""
    print("\n📁 Preparing Vault data directory...")
    c.run(f"ssh root@{VPS_HOST} 'mkdir -p /data/bootstrap/vault/{{file,logs,config}}'")
    c.run(f"ssh root@{VPS_HOST} 'chown -R 1000:1000 /data/bootstrap/vault'")
    c.run(f"ssh root@{VPS_HOST} 'chmod 755 /data/bootstrap/vault'")
    print("✅ Directory preparation complete")


@task
def upload_config(c):
    """Upload Vault configuration"""
    print("\n📤 Uploading Vault configuration...")
    config_file = "bootstrap/05.vault/vault.hcl"
    c.run(f"scp {config_file} root@{VPS_HOST}:/data/bootstrap/vault/config/")
    print("✅ Configuration uploaded")


@task(pre=[prepare, upload_config])
def deploy(c):
    """Deploy Vault to Dokploy (Manual steps)"""
    print("\n🚀 Deploying Vault...")
    print(f"Please use the branch or merge to main in Dokploy, and ensure OP_CONNECT_TOKEN is configured.")
    print(f"Access URL: https://cloud.{INTERNAL_DOMAIN}")
    input("\n✋ Press Enter to continue after completion...")


@task(pre=[deploy])
def init(c):
    """Initialize Vault"""
    print("\n🔐 Initializing Vault...")
    print(f"export VAULT_ADDR=https://vault.{INTERNAL_DOMAIN}")
    print("vault operator init")
    input("\n✋ Press Enter to continue after initialization and saving keys to 1Password...")


@task
def unseal(c):
    """(Manual trigger) Command unsealer container to check status immediately"""
    print("\n🔐 Notifying unsealer container to perform check...")
    c.run(f"ssh root@{VPS_HOST} 'docker logs --tail 20 vault-unsealer'", warn=True)
    c.run(f"ssh root@{VPS_HOST} 'docker restart vault-unsealer'")
    print("✅ Unsealer restarted and first check triggered. Observe logs above.")


@task
def status(c):
    """Check Vault status"""
    print(f"\n🔍 Checking Vault status...")
    c.run(f"curl -s https://vault.{INTERNAL_DOMAIN}/v1/sys/health", warn=True)
    c.run(f"ssh root@{VPS_HOST} 'docker ps | grep vault'", warn=True)


@task(pre=[prepare, upload_config, deploy, init, unseal])
def setup(c):
    """Complete Vault setup flow (including auto-unseal)"""
    print("\n✅ Vault setup complete! Unsealer container will handle future unsealing.")
    print(f"\nAccess URL: https://vault.{INTERNAL_DOMAIN}")
