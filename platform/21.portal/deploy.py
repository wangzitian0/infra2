"""Homer portal deployment"""
import os
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

from libs.deployer import Deployer, make_tasks
from libs.console import env_vars, error, run_with_status, success

# Get shared_tasks from sys.modules (loaded by tools/loader.py)
shared_tasks = sys.modules.get("platform.21.portal.shared")


class PortalDeployer(Deployer):
    service = "portal"
    compose_path = "platform/21.portal/compose.yaml"
    data_path = "/data/platform/portal"
    uid = "1000"
    gid = "1000"
    
    # Domain configuration - disabled to use compose.yaml Traefik labels with SSO
    # When subdomain is None, Dokploy won't auto-configure domain, allowing
    # compose.yaml labels (with forwardauth middleware) to take effect.
    subdomain = None
    service_port = 8080
    service_name = "portal"  # Must match service name in compose.yaml

    @classmethod
    def pre_compose(cls, c):
        if not cls._prepare_dirs(c):
            error("Failed to prepare portal directories")
            return None

        env = cls.env()
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
        config_content = config_content.replace("{{ENV_DOMAIN_SUFFIX}}", domain_suffix)
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

            portal_url = f"https://home{domain_suffix}.{internal_domain}"
            env_vars("PORTAL INFO", {
                "PORTAL_URL": portal_url,
                "CONFIG_PATH": config_path,
                "INTERNAL_DOMAIN": internal_domain,
            })
            success("pre_compose complete")
            result = cls.compose_env_base(env)
            result.update({
                "PORTAL_URL": portal_url,
            })
            return result
        except OSError as exc:
            error("Failed to create temp config", str(exc))
            return None
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)


# Generate tasks
if shared_tasks:
    _tasks = make_tasks(PortalDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
