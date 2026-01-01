"""ClickHouse deployment for SigNoz storage backend"""
import sys
from libs.deployer import Deployer, make_tasks
from libs.console import success, info, run_with_status

shared_tasks = sys.modules.get("platform.03.clickhouse.shared")


class ClickHouseDeployer(Deployer):
    service = "clickhouse"
    compose_path = "platform/03.clickhouse/compose.yaml"
    data_path = "/data/platform/clickhouse"
    uid = "101"  # ClickHouse user in container
    gid = "101"
    
    # No subdomain - internal service only
    subdomain = None
    service_port = None
    service_name = None

    @classmethod
    def pre_compose(cls, c):
        """Prepare directories and ensure secrets exist in Vault."""
        import os
        from libs.console import fatal
        from libs.env import generate_password, get_secrets
        
        if not cls._prepare_dirs(c):
            return None
        
        e = cls.env()
        env_name = e.get('ENV', 'production')
        project = e.get('PROJECT', 'platform')
        
        # Check Vault access
        if not os.getenv('VAULT_ROOT_TOKEN'):
            fatal(
                "VAULT_ROOT_TOKEN not set",
                "Required for storing ClickHouse password\n"
                "   Get token: op read 'op://Infra2/bootstrap%2Fvault%2FRoot%20Token/Root%20Token'\n"
                "   Then: export VAULT_ROOT_TOKEN=<token>"
            )
        
        # Create subdirectories
        subdirs = ["data", "logs", "user_scripts", "zookeeper"]
        for subdir in subdirs:
            run_with_status(
                c, 
                f"ssh root@{e['VPS_HOST']} 'mkdir -p {cls.data_path}/{subdir}'",
                f"Create {subdir} directory"
            )
        
        # Set permissions for ClickHouse directories
        run_with_status(
            c,
            f"ssh root@{e['VPS_HOST']} 'chown -R {cls.uid}:{cls.gid} {cls.data_path}/data {cls.data_path}/logs {cls.data_path}/user_scripts'",
            "Set ClickHouse permissions"
        )
        
        # ZooKeeper needs root (user: root in compose)
        run_with_status(
            c,
            f"ssh root@{e['VPS_HOST']} 'chmod -R 755 {cls.data_path}/zookeeper'",
            "Set ZooKeeper permissions"
        )
        
        # Ensure ClickHouse password exists in Vault
        ch_secrets = get_secrets(project, "clickhouse", env_name)
        password = ch_secrets.get("password")
        if not password:
            password = generate_password(32)
            if not ch_secrets.set("password", password):
                fatal("Failed to store ClickHouse password in Vault")
            info("Generated new ClickHouse password in Vault")
        else:
            info("ClickHouse password exists in Vault")
        
        success("pre_compose complete")
        info("Note: ClickHouse is internal-only (no public domain)")
        
        return {
            "VAULT_ADDR": e.get("VAULT_ADDR", f"https://vault.{e.get('INTERNAL_DOMAIN', 'localhost')}"),
        }


if shared_tasks:
    _tasks = make_tasks(ClickHouseDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
