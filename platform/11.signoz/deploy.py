"""SigNoz deployment - observability platform"""

import os
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

from libs.deployer import Deployer, make_tasks
from libs.console import success, info, run_with_status, error, warning
from libs.common import generate_password

shared_tasks = sys.modules.get("platform.11.signoz.shared")


class SigNozDeployer(Deployer):
    service = "signoz"
    compose_path = "platform/11.signoz/compose.yaml"
    data_path = "/data/platform/signoz"
    # Observability — single prod instance; all envs ship here. No staging copy.
    prod_only = True

    # Routing is compose-owned (explicit Traefik labels in compose.yaml for both the
    # signoz Web UI and the otel-collector public ingest, Infra-014), so Dokploy domain
    # generation MUST be disabled to keep routing single-source (subdomain=None) — see
    # docs/ssot/platform.domain.md and libs/tests/test_domain_routing_policy.py.
    subdomain = None
    service_port = 8080  # SigNoz unified container port
    service_name = "signoz"

    # SigNoz specific secret
    secret_key = "jwt_secret"
    # Infra-014: Vault key (under secret/platform/<env>/signoz) holding the static
    # bearer token that gates the public browser-OTLP ingest domain otel.<domain>.
    otel_ingest_token_key = "otel_ingest_token"

    @classmethod
    def pre_compose(cls, c):
        """Prepare directories and secrets for SigNoz."""
        if not cls._prepare_dirs(c):
            return None

        e = cls.env()
        data_path = cls.data_path_for_env(e)
        host = e.get("VPS_HOST")
        if not host:
            error("Missing VPS_HOST")
            return None
        secrets_backend = cls.secrets()

        # Create data directory for query-service SQLite
        result = run_with_status(
            c, f"ssh root@{host} 'mkdir -p {data_path}/data'", "Create data directory"
        )
        if not result.ok:
            return None

        # Set permissions (SigNoz runs as root in container, but let's be explicit)
        result = run_with_status(
            c, f"ssh root@{host} 'chmod -R 755 {data_path}'", "Set permissions"
        )
        if not result.ok:
            return None

        template_path = Path(__file__).with_name("otel-collector-config.yaml")
        if not template_path.exists():
            error("Missing otel-collector config template", str(template_path))
            return None

        env_suffix = e.get("ENV_SUFFIX") or ""
        config_path = f"{data_path}/otel-collector-config.yaml"

        tmp_path = None
        try:
            config_content = template_path.read_text()
            config_content = config_content.replace("${ENV_SUFFIX}", env_suffix)
            with NamedTemporaryFile("w", delete=False, encoding="utf-8") as tmp:
                tmp.write(config_content)
                tmp_path = tmp.name

            result = run_with_status(
                c,
                f"scp {tmp_path} root@{host}:{config_path}",
                "Upload otel-collector config",
            )
            if not result.ok:
                return None

            result = run_with_status(
                c,
                f"ssh root@{host} 'chmod 644 {config_path}'",
                "Set otel-collector config permissions",
            )
            if not result.ok:
                return None
        except OSError as exc:
            error("Failed to prepare otel-collector config", str(exc))
            return None
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

        if not cls.ensure_runtime_secrets(c):
            return None
        jwt_secret = secrets_backend.get(cls.secret_key)

        # Infra-014: static bearer token for the public browser-OTLP ingest domain
        # otel.${INTERNAL_DOMAIN}. NON-secret publishable ingest key (it is shipped to
        # the browser as NEXT_PUBLIC_*), but we keep it in Vault so it can be rotated
        # without a code change. Auto-provision on first deploy, mirroring the
        # jwt_secret flow above. Consumed by the otel-collector Traefik Header() rule.
        otel_ingest_token = secrets_backend.get(cls.otel_ingest_token_key)
        if not otel_ingest_token:
            otel_ingest_token = generate_password(32)
            if secrets_backend.set(cls.otel_ingest_token_key, otel_ingest_token):
                warning(
                    f"Generated new OTLP ingest token in Vault: {cls.otel_ingest_token_key}"
                )
            else:
                error("Failed to store OTLP ingest token in Vault")
                return None

        success("pre_compose complete")
        domain_suffix = e.get("ENV_DOMAIN_SUFFIX", "")
        info(
            f"Frontend will be available at: https://signoz{domain_suffix}.{e.get('INTERNAL_DOMAIN', 'localhost')}"
        )
        info("OTLP endpoints: 4317 (gRPC), 4318 (HTTP)")
        info(
            f"Public browser-OTLP ingest: https://otel.{e.get('INTERNAL_DOMAIN', 'localhost')}/v1/traces"
        )

        result = cls.compose_env_base(e)
        result["SIGNOZ_JWT_SECRET"] = jwt_secret
        result["OTEL_INGEST_TOKEN"] = otel_ingest_token
        return result


if shared_tasks:
    _tasks = make_tasks(SigNozDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
