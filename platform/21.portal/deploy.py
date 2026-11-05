"""Homer portal deployment"""

import os
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from invoke import task

from common.deploy.config_hash import mount_fingerprint_env
from common.deploy.deployer import Deployer, make_tasks
from common.console import env_vars, error, run_with_status, success, header
from common.core.facets import BackupFacet, Exemption

# Get shared_tasks from sys.modules (loaded by tools/loader.py)
shared_tasks = sys.modules.get("platform.21.portal.shared")


class PortalDeployer(Deployer):
    service = "portal"
    compose_path = "platform/21.portal/compose.yaml"
    data_path = "/data/platform/portal"

    # Backup facts (#542): the backup inventory derives from these
    # (formerly the ops.backup-inventory YAML, deleted).
    backups = (
        BackupFacet(
            method="config_archive",
            restore_command="restore portal config.yml.",
        ),
    )
    secret_key = ""
    uid = "1000"
    gid = "1000"

    # Domain configuration - disabled to use compose.yaml Traefik labels with SSO
    # When subdomain is None, Dokploy won't auto-configure domain, allowing
    # compose.yaml labels (with forwardauth middleware) to take effect.
    subdomain = None
    service_port = 8080
    service_name = "portal"  # Must match service name in compose.yaml

    # No INFRA_PROBE_SPECS probe on purpose (#541; was test_probe_coverage's
    # hand-kept _PROBE_EXEMPT entry, now declared where the service lives):
    exemptions = (
        Exemption(
            check_id="probes",
            reason="static homer dashboard — no liveness endpoint to probe",
        ),
    )

    @classmethod
    def _render_config(cls, env) -> str | None:
        """Render config.yml from its template (pure; none on missing input)."""
        internal_domain = env.get("INTERNAL_DOMAIN")
        if not internal_domain:
            error("Missing INTERNAL_DOMAIN")
            return None
        domain_suffix = env.get("ENV_DOMAIN_SUFFIX", "")

        template_path = Path(__file__).with_name("config.yml.tmpl")
        if not template_path.exists():
            error("Missing config template", str(template_path))
            return None

        config_content = template_path.read_text()
        config_content = config_content.replace("{{INTERNAL_DOMAIN}}", internal_domain)
        return config_content.replace("{{ENV_DOMAIN_SUFFIX}}", domain_suffix)

    @classmethod
    def _deliver_config(cls, c, env) -> str | None:
        """Upload the rendered config.yml to the host.

        Returns the host config path, or None on failure. Shared by pre_compose and
        composing so `sync`, which skips pre_compose, ships a template change (#1438).
        """
        config_content = cls._render_config(env)
        if config_content is None:
            return None
        data_path = cls.data_path_for_env(env)
        config_path = f"{data_path}/config.yml"
        host = env.get("VPS_HOST")
        if not host:
            error("Missing VPS_HOST")
            return None

        tmp_path = None
        try:
            with NamedTemporaryFile("w", delete=False) as tmp:
                tmp.write(config_content)
                tmp_path = tmp.name

            result = run_with_status(
                c,
                f"scp {tmp_path} root@{host}:{config_path}",
                "Upload Homer config",
            )
            if not result.ok:
                return None

            result = run_with_status(
                c,
                f"ssh root@{host} 'chown {cls.uid}:{cls.gid} {config_path}'",
                "Set config ownership",
            )
            if not result.ok:
                return None
            return config_path
        except OSError as exc:
            error("Failed to create temp config", str(exc))
            return None
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    @classmethod
    def composing(cls, c, env_vars):
        """Deliver config.yml, then deploy (`sync` skips pre_compose)."""
        env = cls.env()
        if not cls._deliver_config(c, env):
            raise RuntimeError("Failed to deliver Homer config.yml to host")
        # The label fingerprint recreates the container when config.yml changes (#1438).
        env_vars.update(mount_fingerprint_env({"config.yml": cls._render_config(env)}))
        return super().composing(c, env_vars)

    @classmethod
    def pre_compose(cls, c):
        if not cls._prepare_dirs(c):
            error("Failed to prepare portal directories")
            return None

        env = cls.env()
        config_path = cls._deliver_config(c, env)
        if not config_path:
            return None

        internal_domain = env["INTERNAL_DOMAIN"]
        domain_suffix = env.get("ENV_DOMAIN_SUFFIX", "")
        portal_url = f"https://home{domain_suffix}.{internal_domain}"
        env_vars(
            "PORTAL INFO",
            {
                "PORTAL_URL": portal_url,
                "CONFIG_PATH": config_path,
                "INTERNAL_DOMAIN": internal_domain,
            },
        )
        success("pre_compose complete")
        result = cls.compose_env_base(env)
        result.update(
            {
                "PORTAL_URL": portal_url,
                "INTERNAL_DOMAIN": internal_domain,
            }
        )
        return result


@task
def sso_setup(c):
    """Register Portal in Authentik SSO (One-time)"""
    env = PortalDeployer.env()
    suffix = env.get("ENV_SUFFIX") or ""
    domain_suffix = env.get("ENV_DOMAIN_SUFFIX") or ""
    internal_domain = env.get("INTERNAL_DOMAIN")

    if not internal_domain:
        error("Missing INTERNAL_DOMAIN")
        return

    portal_url = f"https://home{domain_suffix}.{internal_domain}"
    internal_host = f"platform-portal{suffix}"

    header("Portal SSO Setup", f"Registering {portal_url} in Authentik")

    res = run_with_status(
        c, "invoke authentik.shared.create-root-token", "Ensure Root Token"
    )
    if not res.ok:
        return

    res = run_with_status(
        c, "invoke authentik.shared.setup-admin-group", "Ensure Admin Group"
    )
    if not res.ok:
        return

    cmd = (
        f"invoke authentik.shared.create-proxy-app "
        f"--name='Portal' "
        f"--slug='portal' "
        f"--external-host='{portal_url}' "
        f"--internal-host='{internal_host}' "
        f"--port=8080"
    )
    res = run_with_status(c, cmd, "Create SSO Application")
    if not res.ok:
        return

    success("Portal SSO setup complete")


# Generate tasks
if shared_tasks:
    _tasks = make_tasks(PortalDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
