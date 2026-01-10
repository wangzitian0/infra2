"""ClickHouse deployment (simplified - no vault initially)"""
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
    uid = "101"
    gid = "101"
    
    subdomain = None
    service_port = None
    service_name = None

    @classmethod
    def pre_compose(cls, c):
        """Prepare directories for ClickHouse data, logs, and ZooKeeper."""
        if not cls._prepare_dirs(c):
            return None
        
        e = cls.env()
        data_path = cls.data_path_for_env(e)
        ssh_user = e.get("VPS_SSH_USER") or "root"
        suffix = e.get("ENV_SUFFIX", "")
        
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
        
        success("pre_compose complete")
        info("Note: ClickHouse using empty password initially (internal-only)")
        info("Run with vault-agent later for password protection")
        return cls.compose_env_base(e)


if shared_tasks:
    _tasks = make_tasks(ClickHouseDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
