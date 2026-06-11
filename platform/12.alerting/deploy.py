"""Alerting bridge deployment."""

import sys

from libs.deployer import Deployer, make_tasks
from libs.env import get_secrets
from libs.console import error, info, success

shared_tasks = sys.modules.get("platform.12.alerting.shared")


class AlertingDeployer(Deployer):
    service = "alerting"
    compose_path = "platform/12.alerting/compose.yaml"
    data_path = "/data/platform/alerting"

    subdomain = None
    service_port = 8080
    service_name = "feishu-alert-bridge"

    @classmethod
    def compose_env_base(cls, env=None):
        """Include probe heartbeat runtime env in Dokploy compose variables."""
        e = env or cls.env()
        result = super().compose_env_base(e)
        op_secrets = get_secrets(
            project=cls.project_name(e),
            service=cls.service,
            env=e.get("ENV", "production"),
            credential_type="root_vars",
        )
        for key in ("INFRA_PROBE_HEARTBEAT_URL", "INFRA_PROBE_HEARTBEAT_TOKEN"):
            value = op_secrets.get(key)
            if value:
                result[key] = value
        return result

    @classmethod
    def pre_compose(cls, c):
        """Sync 1Password alerting root vars into Vault runtime secrets."""
        if not cls._prepare_dirs(c):
            return None

        if not cls._sync_1password_to_vault():
            return None

        e = cls.env()
        result = cls.compose_env_base(e)
        result["VAULT_ADDR"] = e.get(
            "VAULT_ADDR", f"https://vault.{e.get('INTERNAL_DOMAIN', 'localhost')}"
        )
        success("pre_compose complete - Vault runtime secrets synced from 1Password")
        return result

    @classmethod
    def sync(cls, c, force=False):
        if not cls._sync_1password_to_vault():
            return {"action": "failed", "details": "1Password to Vault sync failed"}
        return super().sync(c, force=force)

    @classmethod
    def verify_runtime_applied(cls, c, env_vars):
        """Confirm the running probe runner actually carries every probe declared
        in the source INFRA_PROBE_SPECS. Closes the gap where a deploy records the
        intended hash but Dokploy never recreated the container, leaving stale
        probe specs while the catalog claims the probes are 'Live'."""
        import time

        import yaml

        from libs.console import warning
        from libs.probe_specs import missing_probe_names

        e = cls.env()
        host = e.get("VPS_HOST")
        if not host:
            warning("verify_runtime_applied: VPS_HOST unset; skipping runtime check")
            return None

        try:
            compose = yaml.safe_load(cls.get_compose_content(c)) or {}
        except Exception as exc:  # noqa: BLE001 - never crash the deploy on a parse issue
            warning(f"verify_runtime_applied: compose parse failed ({exc}); skipping")
            return None
        source_specs = (
            compose.get("services", {})
            .get("infra-probe-runner", {})
            .get("environment", {})
            .get("INFRA_PROBE_SPECS", "")
        )
        if not source_specs:
            return None  # nothing declared to verify

        ssh_user = e.get("VPS_SSH_USER") or "root"
        suffix = e.get("ENV_SUFFIX", "")
        container = f"platform-alerting-probes{suffix}"

        # Retry briefly: a just-recreated container may still be starting.
        last_err = ""
        for attempt in range(3):
            result = c.run(
                f"ssh {ssh_user}@{host} "
                f"'docker exec {container} printenv INFRA_PROBE_SPECS'",
                warn=True,
                hide=True,
            )
            if not result.failed:
                missing = missing_probe_names(source_specs, result.stdout or "")
                if not missing:
                    success(f"Runtime verified: {container} carries all source probes")
                    return None
                last_err = (
                    f"{container} is missing {len(missing)} probe(s) from the deployed "
                    f"INFRA_PROBE_SPECS ({', '.join(missing)}) — container did not pick "
                    "up the new config (likely not recreated)"
                )
            else:
                last_err = (
                    f"could not read INFRA_PROBE_SPECS from {container}: "
                    f"{(result.stderr or 'docker exec failed').strip()}"
                )
            if attempt < 2:
                time.sleep(5)
        return last_err

    @classmethod
    def _sync_1password_to_vault(cls) -> bool:
        e = cls.env()
        env_name = e.get("ENV", "production")
        project = cls.project_name(e)

        op_secrets = get_secrets(
            project=project,
            service=cls.service,
            env=env_name,
            credential_type="root_vars",
        )
        vault_secrets = get_secrets(
            project=project,
            service=cls.service,
            env=env_name,
            credential_type="app_vars",
        )

        mode = op_secrets.get("ALERT_DELIVERY_MODE") or "feishu_webhook"
        required_by_mode = {
            "feishu_webhook": ["FEISHU_WEBHOOK_URL"],
            "feishu_app": ["FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_CHAT_ID"],
        }
        if mode not in required_by_mode:
            error(f"Unsupported ALERT_DELIVERY_MODE in 1Password: {mode}")
            return False

        keys = [
            "ALERT_DELIVERY_MODE",
            "FEISHU_WEBHOOK_URL",
            "FEISHU_APP_ID",
            "FEISHU_APP_SECRET",
            "FEISHU_CHAT_ID",
            "FEISHU_API_BASE",
            "BRIDGE_BASIC_AUTH_USERNAME",
            "BRIDGE_BASIC_AUTH_PASSWORD",
            "INFRA_PROBE_HEARTBEAT_URL",
            "INFRA_PROBE_HEARTBEAT_TOKEN",
        ]
        values = {key: op_secrets.get(key) for key in keys}
        values["ALERT_DELIVERY_MODE"] = mode

        missing = [key for key in required_by_mode[mode] if not values.get(key)]
        if missing:
            error(
                "Missing alerting secrets in 1Password root_vars",
                f"item={project}/{env_name}/{cls.service} keys={', '.join(missing)}",
            )
            return False

        for key, value in values.items():
            if value is None:
                continue
            if not vault_secrets.set(key, value):
                error(f"Failed to sync {key} into Vault runtime secret")
                return False

        info(f"Alerting delivery mode: {mode}")
        success("Synced alerting runtime secrets from 1Password to Vault")
        return True


if shared_tasks:
    _tasks = make_tasks(AlertingDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
