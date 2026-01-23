"""Prefect deployment with vault-init"""

import sys
import os
from libs.deployer import Deployer, make_tasks
from libs.common import with_env_suffix
from libs.console import success, info, fatal
from libs.env import get_secrets

shared_tasks = sys.modules.get("platform.23.prefect.shared")


class PrefectDeployer(Deployer):
    service = "prefect"
    compose_path = "platform/23.prefect/compose.yaml"
    data_path = None
    uid = "999"
    gid = "999"
    secret_key = "postgres_password"

    subdomain = None
    service_port = 4200
    service_name = "prefect-server"

    @classmethod
    def pre_compose(cls, c):
        """Prepare directories, check dependencies, ensure secrets exist in Vault."""
        if not cls._prepare_dirs(c):
            return None

        e = cls.env()
        env_name = e.get("ENV", "production")
        project = e.get("PROJECT", "platform")

        if not os.getenv("VAULT_ROOT_TOKEN"):
            fatal(
                "VAULT_ROOT_TOKEN not set",
                "Required for reading postgres password\n"
                "   Get token: op read 'op://Infra2/dexluuvzg5paff3cltmtnlnosm/Root Token'\n"
                "   Then: export VAULT_ROOT_TOKEN=<token>",
            )

        pg_secrets = get_secrets(project, "postgres", env_name)
        if not pg_secrets.get("root_password"):
            fatal(
                "Postgres password not found in Vault",
                "Run: export VAULT_ROOT_TOKEN=<token> && invoke postgres.setup",
            )

        redis_secrets = get_secrets(project, "redis", env_name)
        if not redis_secrets.get("password"):
            fatal(
                "Redis password not found in Vault",
                "Run: export VAULT_ROOT_TOKEN=<token> && invoke redis.setup",
            )
        success("Verified postgres and redis secrets in Vault")

        postgres_container = with_env_suffix("platform-postgres", e)
        result = c.run(
            f"ssh root@{e['VPS_HOST']} \"docker exec {postgres_container} psql -U postgres -c 'CREATE DATABASE prefect;'\"",
            warn=True,
            hide=True,
        )
        if result.failed:
            stderr = result.stderr or ""
            if "already exists" in stderr:
                info("Database 'prefect' already exists")
            else:
                fatal("Failed to create 'prefect' database", f"Error: {stderr}")
        else:
            success("Database created")

        result = cls.compose_env_base(e)
        result["VAULT_ADDR"] = e.get(
            "VAULT_ADDR", f"https://vault.{e.get('INTERNAL_DOMAIN', 'localhost')}"
        )

        success("pre_compose complete - vault-init will fetch secrets at runtime")
        info("\nNote: VAULT_APP_TOKEN auto-configured via 'invoke vault.setup-tokens'")
        return result

    @classmethod
    def post_compose(cls, c, shared_tasks):
        """Verify deployment and setup SSO protection"""
        from libs.console import header, success, info

        header(f"{cls.service} post_compose", "Verifying")
        result = shared_tasks.status(c)
        if result["is_ready"]:
            success(f"post_compose complete - {result['details']}")

            e = cls.env()
            env_domain_suffix = e.get("ENV_DOMAIN_SUFFIX", "")
            internal_domain = e.get("INTERNAL_DOMAIN")

            info("\n📌 Next: Configure SSO protection in Authentik")
            info("   invoke authentik.shared.create-proxy-app \\")
            info("     --name=Prefect \\")
            info("     --slug=prefect \\")
            info(
                f"     --external-host=https://prefect{env_domain_suffix}.{internal_domain} \\"
            )
            info(
                f"     --internal-host=platform-prefect-server{e.get('ENV_SUFFIX', '')} \\"
            )
            info("     --port=4200")
            return True
        return False


if shared_tasks:
    _tasks = make_tasks(PrefectDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
