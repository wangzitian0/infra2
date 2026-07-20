import hashlib
import json
import re
import sys
import time
from pathlib import Path

from libs.console import error
from libs.deploy.deployer import Deployer, make_tasks
from libs.service_facets import BackupFacet, SecretsFacet

shared_tasks = sys.modules.get("truealpha.20.data_engine.shared")

_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_RELEASE_ID = re.compile(r"^release-manifest:[0-9a-f]{64}$")


class DataEngineDeployer(Deployer):
    """TrueAlpha Dagster control plane and real-source execution runtime."""

    service = "data_engine"
    compose_path = "truealpha/truealpha/20.data_engine/compose.yaml"
    data_path = "/data/truealpha/dagster"

    # Backup facts (#542): the backup inventory derives from these
    # (formerly the ops.backup-inventory YAML, deleted).
    backups = (
        BackupFacet(
            method="dagster_artifact_archive",
            restore_command="restore optional Dagster compute logs/IO artifacts; authoritative run metadata and raw evidence recover from Postgres and S3.",
        ),
    )
    uid = "10001"
    gid = "10001"
    secret_key = ""
    project = "truealpha"

    subdomain = None
    service_port = 3001
    service_name = "dagster-webserver"

    # Rollout state (#500/#522/#542): staging-scoped, no production composes or
    # Vault provisioning yet (see truealpha/01.postgres/deploy.py's twin attr
    # for the full context). REMOVE when promoted to production.
    not_yet_in_production = True

    # Vault self-refresh facts (#542): the audit inventory derives from this
    # (AppRole auth from day one).
    secrets = (
        SecretsFacet(
            vault_agent_container="truealpha-data-engine-vault-agent${ENV_SUFFIX}",
            app_containers=(
                "truealpha-dagster-webserver${ENV_SUFFIX}",
                "truealpha-dagster-daemon${ENV_SUFFIX}",
            ),
            auth_method="approle",
        ),
    )

    _POSTGRES_PORTS = {"staging": "15432", "production": "15433"}
    _WEBSERVER_PORTS = {"staging": "13001", "production": "13002"}
    _REQUIRED_SECRET_KEYS = (
        "SEC_USER_AGENT",
        "S3_ENDPOINT",
        "S3_ACCESS_KEY",
        "S3_SECRET_KEY",
        "S3_BUCKET",
        "DATA_ENGINE_IMAGE_DIGEST",
        "RELEASE_MANIFEST_ID",
        "CAPTURE_APPROVED_BY",
    )

    @classmethod
    def ensure_runtime_secrets(cls, c=None) -> bool:
        secrets = cls.secrets_backend()
        missing = [key for key in cls._REQUIRED_SECRET_KEYS if not secrets.get(key)]
        if missing:
            error(f"Missing TrueAlpha data-engine Vault fields: {', '.join(missing)}")
            return False
        image_digest = secrets.get("DATA_ENGINE_IMAGE_DIGEST") or ""
        release_id = secrets.get("RELEASE_MANIFEST_ID") or ""
        if not _IMAGE_DIGEST.fullmatch(image_digest):
            error("DATA_ENGINE_IMAGE_DIGEST must be a full sha256 OCI digest")
            return False
        if not _RELEASE_ID.fullmatch(release_id):
            error("RELEASE_MANIFEST_ID must be a content-addressed release-manifest ID")
            return False
        return True

    @classmethod
    def compose_env_base(cls, env: dict | None = None) -> dict[str, str]:
        base = super().compose_env_base(env)
        secrets = cls.secrets_backend()
        environment = base.get("ENV", "production")
        image_digest = secrets.get("DATA_ENGINE_IMAGE_DIGEST") or ""
        release_id = secrets.get("RELEASE_MANIFEST_ID") or ""
        approved_by = secrets.get("CAPTURE_APPROVED_BY") or ""
        if not _IMAGE_DIGEST.fullmatch(image_digest):
            raise ValueError(
                "DATA_ENGINE_IMAGE_DIGEST must be configured before deployment"
            )
        if not _RELEASE_ID.fullmatch(release_id):
            raise ValueError("RELEASE_MANIFEST_ID must be configured before deployment")
        if not approved_by:
            raise ValueError("CAPTURE_APPROVED_BY must be configured before deployment")

        base.update(
            {
                "TA_POSTGRES_PORT": cls._POSTGRES_PORTS.get(environment, "0"),
                "DAGSTER_WEBSERVER_PORT": cls._WEBSERVER_PORTS.get(environment, "0"),
                "DATA_ENGINE_IMAGE_DIGEST": image_digest,
                "RELEASE_MANIFEST_ID": release_id,
                "CAPTURE_APPROVED_BY": approved_by,
                "GIT_COMMIT_SHA": secrets.get("GIT_COMMIT_SHA") or "unknown",
                "TIER_CPU_SHARES": "512" if environment == "staging" else "1024",
                "DATA_ENGINE_MEM_LIMIT": "768m"
                if environment == "staging"
                else "1536m",
                "DATA_ENGINE_VAULT_MEM_LIMIT": "128m",
            }
        )
        base["CONFIGURATION_SHA256"] = cls._configuration_sha256(base)
        return base

    @classmethod
    def _configuration_sha256(cls, public_env: dict[str, str]) -> str:
        directory = Path(__file__).resolve().parent
        artifacts = []
        for name in (
            "compose.yaml",
            "dagster-entrypoint.sh",
            "secrets.ctmpl",
            "vault-agent.hcl",
            "vault-policy.hcl",
        ):
            path = directory / name
            artifacts.append((name, hashlib.sha256(path.read_bytes()).hexdigest()))
        payload = {
            "public_env": dict(sorted(public_env.items())),
            "artifacts": artifacts,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @classmethod
    def verify_runtime_applied(cls, c, env_vars: dict[str, str]) -> str | None:
        expected_digest = env_vars["DATA_ENGINE_IMAGE_DIGEST"]
        expected_image = f"ghcr.io/wangzitian0/truealpha-data-engine@{expected_digest}"
        e = cls.env()
        host = e.get("VPS_HOST")
        if not host:
            return "VPS_HOST is unavailable for runtime image verification"
        ssh_user = e.get("VPS_SSH_USER") or "root"
        suffix = e.get("ENV_SUFFIX") or ""
        containers = (
            f"truealpha-dagster-webserver{suffix}",
            f"truealpha-dagster-daemon{suffix}",
        )
        deadline = time.monotonic() + 90
        last_error = "containers did not expose the promoted image"
        while time.monotonic() < deadline:
            mismatches = []
            for container in containers:
                result = c.run(
                    f"ssh {ssh_user}@{host} \"docker inspect -f '{{{{.Config.Image}}}}' {container}\"",
                    warn=True,
                    hide=True,
                )
                actual = (result.stdout or "").strip()
                if not result.ok or actual != expected_image:
                    mismatches.append(f"{container}={actual or 'unavailable'}")
            if not mismatches:
                return None
            last_error = ", ".join(mismatches)
            time.sleep(3)
        return f"promoted image digest was not applied: {last_error}"


if shared_tasks:
    _tasks = make_tasks(DataEngineDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
