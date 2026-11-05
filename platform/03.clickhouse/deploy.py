"""ClickHouse deployment (simplified - no vault initially)"""

import os
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

from common.deploy.config_hash import mount_fingerprint_env
from common.deploy.deployer import Deployer, make_tasks
from common.console import success, info, run_with_status, error
from common.core.facets import BackupFacet, Exemption

shared_tasks = sys.modules.get("platform.03.clickhouse.shared")


class ClickHouseDeployer(Deployer):
    service = "clickhouse"
    secret_key = ""
    compose_path = "platform/03.clickhouse/compose.yaml"
    data_path = "/data/platform/clickhouse"

    # Backup facts (#542): the backup inventory derives from these
    # (formerly the ops.backup-inventory YAML, deleted).
    backups = (
        BackupFacet(
            method="clickhouse_data_archive",
            restore_command="restore ClickHouse data/log directories while service is stopped.",
        ),
    )
    # Backs SigNoz (observability) — single prod instance, no staging copy.
    prod_only = True
    uid = "101"
    gid = "101"

    subdomain = None
    service_port = None
    service_name = None

    # No INFRA_PROBE_SPECS probe on purpose (#541 facet form of the decision
    # previously documented inline in the alerting compose literal):
    exemptions = (
        Exemption(
            check_id="probes",
            reason="a read-only /ping stays 200 even when the data dir is "
            "unwritable (read green / write dead). The truthful signals "
            "replaced it: op-ch & platform-clickhouse have WRITE-PATH "
            "healthchecks (unhealthy -> Dokploy restart), and the signoz/"
            "openpanel round-trip probes are the alert path (write+query, so "
            "they catch write-broken AND down). A /ping caught neither better, "
            "so it was removed rather than kept as dead compensation.",
        ),
    )

    @classmethod
    def _render_config(cls, e) -> str | None:
        """Render config.xml from its template (pure; none when the template is missing)."""
        suffix = e.get("ENV_SUFFIX", "")
        template_path = Path(__file__).with_name("config.xml")
        if not template_path.exists():
            error("Missing ClickHouse config template", str(template_path))
            return None
        config_content = template_path.read_text()
        config_content = config_content.replace(
            "{{CLICKHOUSE_HOST}}", f"platform-clickhouse{suffix}"
        )
        return config_content.replace(
            "{{ZOOKEEPER_HOST}}", f"platform-clickhouse-zookeeper{suffix}"
        )

    @classmethod
    def _deliver_config(cls, c, e) -> bool:
        """Upload the rendered config.xml to the host (idempotent)."""
        data_path = cls.data_path_for_env(e)
        ssh_user = e.get("VPS_SSH_USER") or "root"

        config_content = cls._render_config(e)
        if config_content is None:
            return False
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
                return False

            run_with_status(
                c,
                f"ssh {ssh_user}@{e['VPS_HOST']} 'chown {cls.uid}:{cls.gid} {config_path}'",
                "Set config ownership",
            )
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
        return True

    @classmethod
    def composing(cls, c, env_vars):
        """Deliver config.xml, then deploy (`sync` skips pre_compose)."""
        e = cls.env()
        if not cls._deliver_config(c, e):
            raise RuntimeError("Failed to deliver ClickHouse config.xml to host")
        # The label fingerprint recreates the container when config.xml changes (#1438).
        env_vars.update(
            mount_fingerprint_env(
                {"config.xml": cls._render_config(e)},
                service=cls.service,
                template_dir=Path(__file__).parent,
            )
        )
        return super().composing(c, env_vars)

    @classmethod
    def pre_compose(cls, c):
        """Prepare directories for ClickHouse data, logs, and ZooKeeper."""
        if not cls._prepare_dirs(c):
            return None

        e = cls.env()
        data_path = cls.data_path_for_env(e)

        # Create subdirectories
        subdirs = ["data", "logs", "user_scripts", "zookeeper"]
        for subdir in subdirs:
            run_with_status(
                c,
                f"ssh root@{e['VPS_HOST']} 'mkdir -p {data_path}/{subdir}'",
                f"Create {subdir} directory",
            )

        # Set permissions for ClickHouse directories
        run_with_status(
            c,
            f"ssh root@{e['VPS_HOST']} 'chown -R {cls.uid}:{cls.gid} {data_path}/data {data_path}/logs {data_path}/user_scripts'",
            "Set ClickHouse permissions",
        )

        # ZooKeeper needs root (user: root in compose)
        run_with_status(
            c,
            f"ssh root@{e['VPS_HOST']} 'chmod -R 755 {data_path}/zookeeper'",
            "Set ZooKeeper permissions",
        )

        # #1438: config delivery is shared with composing() so `sync`, which skips
        # pre_compose, also ships a config.xml template change.
        if not cls._deliver_config(c, e):
            return None

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
