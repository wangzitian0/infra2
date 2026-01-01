"""MinIO deployment with vault-init

Root credentials pattern:
- Stored in 1Password for Web Console login (human access)
- Synced to Vault for vault-agent (machine access)
"""
import subprocess
import sys
from libs.deployer import Deployer, make_tasks
from libs.env import generate_password, get_secrets, OpSecrets
from libs.console import header, success, error, warning, info, env_vars

shared_tasks = sys.modules.get("platform.03.minio.shared")


class MinioDeployer(Deployer):
    service = "minio"
    compose_path = "platform/03.minio/compose.yaml"
    data_path = "/data/platform/minio"
    secret_key = "root_password"
    
    # Domain configuration for Dokploy
    subdomain = "s3"  # s3.{INTERNAL_DOMAIN}
    service_port = 9001  # MinIO Console port
    service_name = "minio"
    
    # 1Password item for root credentials
    OP_ITEM = "platform/minio/admin"
    
    @classmethod
    def pre_compose(cls, c):
        """Prepare directories and sync root credentials to both 1Password and Vault."""
        if not cls._prepare_dirs(c):
            return None
        
        e = cls.env()
        env_name = e.get('ENV', 'production')
        
        header(f"{cls.service} pre_compose", "Setting up root credentials")
        
        # Get or generate root credentials
        vault_secrets = cls.secrets()
        root_user = vault_secrets.get("root_user") or "admin"
        root_password = vault_secrets.get("root_password")
        
        if not root_password:
            root_password = generate_password(32)
            warning("Generated new MinIO root password")
        
        # Store in Vault (for vault-agent)
        if vault_secrets.set("root_user", root_user):
            info("Vault: root_user stored")
        else:
            error("Failed to store root_user in Vault")
            return None
            
        if vault_secrets.set("root_password", root_password):
            info("Vault: root_password stored")
        else:
            error("Failed to store root_password in Vault")
            return None
        
        # Store in 1Password (for Web Console login)
        cls._sync_to_1password(root_user, root_password)
        
        # Return VAULT_ADDR for vault-init pattern
        result = {
            "VAULT_ADDR": e.get("VAULT_ADDR", f"https://vault.{e.get('INTERNAL_DOMAIN', 'localhost')}"),
        }
        
        env_vars("DOKPLOY ENV (vault-init)", result)
        success("pre_compose complete")
        info(f"MinIO Console: https://{cls.subdomain}.{e.get('INTERNAL_DOMAIN')}")
        info(f"Login: {root_user} / (password in 1Password)")
        return result
    
    @classmethod
    def _sync_to_1password(cls, username: str, password: str) -> bool:
        """Sync root credentials to 1Password for Web Console access."""
        try:
            # Check if item exists
            result = subprocess.run(
                ['op', 'item', 'get', cls.OP_ITEM, '--vault=Infra2', '--format=json'],
                capture_output=True, text=True
            )
            
            if result.returncode == 0:
                # Update existing item
                subprocess.run(
                    ['op', 'item', 'edit', cls.OP_ITEM, '--vault=Infra2',
                     f'username={username}', f'password={password}'],
                    capture_output=True, check=True
                )
                info("1Password: Updated existing item")
            else:
                # Create new item
                subprocess.run(
                    ['op', 'item', 'create', '--category=login', f'--title={cls.OP_ITEM}',
                     '--vault=Infra2', f'username={username}', f'password={password}'],
                    capture_output=True, check=True
                )
                success("1Password: Created new item")
            return True
        except subprocess.CalledProcessError as exc:
            warning(f"1Password sync failed: {exc}")
            info("You can manually create: op item create --category=login ...")
            return False


if shared_tasks:
    _tasks = make_tasks(MinioDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
