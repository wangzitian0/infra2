"""OpenPanel deployment with vault-init"""

import sys
import os
from libs.deployer import Deployer, make_tasks
from libs.common import with_env_suffix
from libs.console import success, warning, info, error
from libs.env import get_secrets

shared_tasks = sys.modules.get("platform.24.openpanel.shared")


class OpenPanelDeployer(Deployer):
    service = "openpanel"
    compose_path = "platform/24.openpanel/compose.yaml"
    data_path = "/data/platform/openpanel"
    uid = "1000"
    gid = "1000"
    secret_key = "encryption_key"

    # Domain configuration - None to use compose.yaml Traefik labels
    subdomain = None
    service_port = 3000
    service_name = "op-dashboard"

    @classmethod
    def ensure_runtime_secrets(cls, c=None) -> bool:
        """Ensure every Vault key referenced by secrets.ctmpl exists."""
        e = cls.env()
        env_name = e.get("ENV", "production")
        project = e.get("PROJECT", "platform")

        pg_secrets = get_secrets(project, "postgres", env_name)
        redis_secrets = get_secrets(project, "redis", env_name)
        if not pg_secrets.get("root_password"):
            warning("Postgres password not found in Vault")
            return False
        if not redis_secrets.get("password"):
            warning("Redis password not found in Vault")
            return False

        openpanel_secrets = get_secrets(project, "openpanel", env_name)

        encryption_key = openpanel_secrets.get("encryption_key")
        if not encryption_key:
            import secrets as py_secrets

            # OpenPanel encryption key must be 32-byte hex string (64 characters)
            encryption_key = py_secrets.token_hex(32)
            if not openpanel_secrets.set("encryption_key", encryption_key):
                error("Failed to store encryption key in Vault")
                return False
            warning("Generated new encryption key in Vault")
        else:
            info("Encryption key exists in Vault")

        # Check or set resend_api_key placeholder
        resend_api_key = openpanel_secrets.get("resend_api_key")
        if not resend_api_key:
            openpanel_secrets.set("resend_api_key", "placeholder")
            warning("Set placeholder resend_api_key in Vault")

        return True

    @classmethod
    def pre_compose(cls, c):
        """Prepare directories, check dependencies, ensure secrets exist in Vault."""
        from libs.console import fatal

        if not cls._prepare_dirs(c):
            return None

        e = cls.env()
        env_name = e.get("ENV", "production")
        project = e.get("PROJECT", "platform")

        # Check Vault access
        if not os.getenv("VAULT_ROOT_TOKEN"):
            fatal(
                "VAULT_ROOT_TOKEN not set",
                "Required for: 1) Reading postgres/redis passwords, 2) Storing openpanel secrets\n"
                "   Get token: op read 'op://Infra2/dexluuvzg5paff3cltmtnlnosm/Root Token' "
                "(or /Token; item: bootstrap/vault/Root Token)\n"
                "   Then: export VAULT_ROOT_TOKEN=<token>",
            )

        # Check dependencies exist in Vault
        pg_secrets = get_secrets(project, "postgres", env_name)
        redis_secrets = get_secrets(project, "redis", env_name)

        if not pg_secrets.get("root_password"):
            fatal(
                "Postgres password not found in Vault",
                "Run: export VAULT_ROOT_TOKEN=<token> && invoke postgres.setup",
            )
        if not redis_secrets.get("password"):
            fatal(
                "Redis password not found in Vault",
                "Run: export VAULT_ROOT_TOKEN=<token> && invoke redis.setup",
            )
        success("Verified postgres and redis secrets in Vault")

        # Create Postgres database (idempotent)
        postgres_container = with_env_suffix("platform-postgres", e)
        result = c.run(
            f"ssh root@{e['VPS_HOST']} \"docker exec {postgres_container} psql -U postgres -c 'CREATE DATABASE openpanel;'\"",
            warn=True,
            hide=True,
        )
        if result.failed:
            stderr = result.stderr or ""
            if "already exists" in stderr:
                info("Database 'openpanel' already exists in Postgres")
            else:
                fatal(
                    "Failed to create 'openpanel' database in Postgres",
                    f"Error: {stderr}",
                )
        else:
            success("Database 'openpanel' created in Postgres")

        # Create ClickHouse database (idempotent)
        clickhouse_container = with_env_suffix("platform-clickhouse", e)
        result = c.run(
            f"ssh root@{e['VPS_HOST']} \"docker exec {clickhouse_container} clickhouse-client -q 'CREATE DATABASE IF NOT EXISTS openpanel;'\"",
            warn=True,
            hide=True,
        )
        if result.failed:
            stderr = result.stderr or ""
            fatal(
                "Failed to create 'openpanel' database in ClickHouse",
                f"Error: {stderr}",
            )
        else:
            success("Database 'openpanel' created/verified in ClickHouse")

        if not cls.ensure_runtime_secrets(c):
            fatal("Failed to ensure OpenPanel runtime secrets")

        # Return VAULT_ADDR for vault-init pattern
        result = cls.compose_env_base(e)
        result["VAULT_ADDR"] = e.get(
            "VAULT_ADDR", f"https://vault.{e.get('INTERNAL_DOMAIN', 'localhost')}"
        )

        success("pre_compose complete - vault-init will fetch secrets at runtime")
        info("\nNote: VAULT_APP_TOKEN auto-configured via 'invoke vault.setup-tokens'")
        return result

    @classmethod
    def post_compose(cls, c, shared_tasks):
        """Verify deployment"""
        from libs.console import header, success, error

        header(f"{cls.service} post_compose", "Verifying")
        result = shared_tasks.status(c)
        if result["is_ready"]:
            success(f"post_compose complete - {result['details']}")
            return True
        error("Verification failed", result["details"])
        return False


if shared_tasks:
    _tasks = make_tasks(OpenPanelDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
