"""ClickHouse deployment (simplified - no vault initially)"""
import sys
from libs.deployer import Deployer, make_tasks
from libs.console import success, info, run_with_status

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
        
        success("pre_compose complete")
        info("Note: ClickHouse using empty password initially (internal-only)")
        info("Run with vault-agent later for password protection")
        return {}


if shared_tasks:
    _tasks = make_tasks(ClickHouseDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
