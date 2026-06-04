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
