"""ClickHouse deployment for SigNoz storage backend"""
import os
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

from libs.deployer import Deployer, make_tasks
from libs.console import success, info, run_with_status, error

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
        from libs.console import fatal
        from libs.env import generate_password, get_secrets
        
        if not cls._prepare_dirs(c):
            return None
        
        e = cls.env()
        data_path = cls.data_path_for_env(e)
        ssh_user = e.get("VPS_SSH_USER") or "root"
        suffix = e.get("ENV_SUFFIX", "")
        env_name = e.get('ENV', 'production')
        project = e.get('PROJECT', 'platform')
        
        # Check Vault access
        if not os.getenv('VAULT_ROOT_TOKEN'):
            fatal(
                "VAULT_ROOT_TOKEN not set",
                "Required for storing ClickHouse password\n"
                "   Get token: op read 'op://Infra2/dexluuvzg5paff3cltmtnlnosm/Root Token' "
                "(or /Token; item: bootstrap/vault/Root Token)\n"
                "   Then: export VAULT_ROOT_TOKEN=<token>"
            )
        
        # Create subdirectories
        subdirs = ["data", "logs", "user_scripts", "zookeeper"]
        for subdir in subdirs:
            run_with_status(
                c, 
                f"ssh root@{e['VPS_HOST']} 'mkdir -p {data_path}/{subdir}'",
                f"Create {subdir} directory"
            )
        
        # Set permissions for ClickHouse directories
        run_with_status(
            c,
            f"ssh root@{e['VPS_HOST']} 'chown -R {cls.uid}:{cls.gid} {data_path}/data {data_path}/logs {data_path}/user_scripts'",
            "Set ClickHouse permissions"
        )
        
        # ZooKeeper needs root (user: root in compose)
        run_with_status(
            c,
            f"ssh root@{e['VPS_HOST']} 'chmod -R 755 {data_path}/zookeeper'",
            "Set ZooKeeper permissions"
        )

        template_path = Path(__file__).with_name("config.xml")
        if not template_path.exists():
            error("Missing ClickHouse config template", str(template_path))
            return None

        config_content = template_path.read_text()
        config_content = config_content.replace("{{CLICKHOUSE_HOST}}", f"platform-clickhouse{suffix}")
        config_content = config_content.replace("{{ZOOKEEPER_HOST}}", f"platform-clickhouse-zookeeper{suffix}")
        config_path = f"{data_path}/config.xml"

        tmp_path = None
        try:
            with NamedTemporaryFile("w", delete=False) as tmp:
                tmp.write(config_content)
                tmp_path = tmp.name

            result = run_with_status(
                c,
                f"scp {tmp_path} {ssh_user}@{e['VPS_HOST']}:{config_path}",
                "Upload ClickHouse config",
            )
            if not result.ok:
                return None

            run_with_status(
                c,
                f"ssh {ssh_user}@{e['VPS_HOST']} 'chown {cls.uid}:{cls.gid} {config_path}'",
                "Set config ownership",
            )
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
        
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
        
        result = cls.compose_env_base(e)
        result["VAULT_ADDR"] = e.get("VAULT_ADDR", f"https://vault.{e.get('INTERNAL_DOMAIN', 'localhost')}")
        return result


if shared_tasks:
    _tasks = make_tasks(ClickHouseDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
