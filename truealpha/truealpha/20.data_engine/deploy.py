import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from invoke.exceptions import CommandTimedOut
from libs.console import error, success
from libs.deploy.deployer import Deployer, make_tasks
from libs.service_facets import BackupFacet, SecretsFacet

shared_tasks = sys.modules.get("truealpha.20.data_engine.shared")

_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERSION_REF = re.compile(r"^(v[0-9]+\.[0-9]+\.[0-9]+|[0-9a-f]{7,40})$")
_IMAGE = "ghcr.io/wangzitian0/truealpha-data-engine"
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

    # Values sourced from the independently released TrueAlpha artifact/runtime
    # approval plane affect deployment idempotence, but cannot be reconstructed
    # from an infra2 release.  Declaring them forces a separate secret-free
    # source identity instead of letting the drift runner read Vault.
    runtime_only_config_keys = frozenset(
        {
            "DATA_ENGINE_IMAGE_DIGEST",
            "RELEASE_MANIFEST_ID",
            "CAPTURE_APPROVED_BY",
            "GIT_COMMIT_SHA",
            "CONFIGURATION_SHA256",
        }
    )

    # Rollout state: graduated to production 2026-07-27 (owner-approved,
    # CAPTURE_APPROVED_BY recorded in secret/truealpha/production/data_engine).
    # The former `not_yet_in_production = True` staging scope (#500/#522/#542)
    # is removed — prod reconcile fan-out and the Vault self-refresh audit now
    # include this service, mirroring truealpha/01.postgres's graduation.

    # Vault self-refresh facts (#542): the audit inventory derives from this
    # (AppRole auth from day one).
    secrets = (
        SecretsFacet(
            vault_agent_container="truealpha-data-engine-vault-agent${ENV_SUFFIX}",
            app_containers=(
                "truealpha-dagster-webserver${ENV_SUFFIX}",
                "truealpha-dagster-daemon${ENV_SUFFIX}",
                "truealpha-dagster-code-server${ENV_SUFFIX}",
            ),
            auth_method="approle",
        ),
    )

    _POSTGRES_PORTS = {"staging": "15432", "production": "15433"}
    # MinIO's S3 host-loopback publish (platform/03.minio `_S3_HOST_PORTS`, #602).
    # This service runs `network_mode: host` for OpenD, so it reaches platform
    # services only through the host loopback, never the dokploy overlay. Mirrored
    # rather than imported: the two stacks deploy independently, and a shared
    # constant would imply an ordering that does not exist. A test asserts they agree.
    _MINIO_S3_PORTS = {"staging": "19000", "production": "19001"}
    _WEBSERVER_PORTS = {"staging": "13001", "production": "13002"}
    # S3_ENDPOINT is deliberately absent: the template no longer reads it. Requiring
    # a Vault key nothing consumes is worse than not requiring it — it invites
    # someone to "fix" the endpoint there and observe no effect.
    _REQUIRED_SECRET_KEYS = (
        "SEC_USER_AGENT",
        "S3_ACCESS_KEY",
        "S3_SECRET_KEY",
        "S3_BUCKET",
        "DATA_ENGINE_IMAGE_DIGEST",
        "RELEASE_MANIFEST_ID",
        "CAPTURE_APPROVED_BY",
    )

    @classmethod
    def pin_release(cls, version_ref: str, *, secrets=None, resolve=None) -> str:
        """Pin the data-engine image of app release ``version_ref`` in Vault (truealpha#712).

        The stack is digest-pinned; a release request carries a tag. The runner — the
        one deploy context with the AppRole that may update this env's data_engine
        path — resolves ``truealpha-data-engine:<tag>`` to its registry digest and
        writes ``DATA_ENGINE_IMAGE_DIGEST`` plus ``GIT_COMMIT_SHA`` (the tag, the same
        identifier the app images stamp) before ``compose_env_base`` reads them. The
        existing ``verify_runtime_applied`` then proves the three containers run that
        digest — the post-deploy assertion the issue asked for, unchanged.

        Fails closed: an unresolvable tag or a refused Vault write raises, and the
        deploy stops before any compose mutation.
        """
        from libs.image_digest import resolve_image_digest

        candidate = str(version_ref).strip()
        if not _VERSION_REF.fullmatch(candidate):
            raise ValueError(
                f"DEPLOY_VERSION_REF must be a vX.Y.Z tag or a commit sha, got {version_ref!r}"
            )
        digest = (resolve or resolve_image_digest)(_IMAGE, candidate)
        if not _IMAGE_DIGEST.fullmatch(digest):
            raise ValueError(
                f"registry returned a malformed digest for {candidate}: {digest!r}"
            )
        backend = secrets if secrets is not None else cls.secrets_backend()
        for key, value in (
            ("DATA_ENGINE_IMAGE_DIGEST", digest),
            ("GIT_COMMIT_SHA", candidate),
        ):
            if not backend.set(key, value):
                raise RuntimeError(
                    f"Vault refused to pin {key} for release {candidate}"
                )
        success(f"{cls.service}: pinned release {candidate} -> {digest[:19]}… in Vault")
        return digest

    @classmethod
    def ensure_runtime_secrets(cls, c=None) -> bool:
        # From the PROCESS environment, not cls.env(): Deployer.env() is the curated
        # deployment config (libs.common.get_env — 1Password init vars plus a fixed set
        # of os.environ keys), and the runner's per-deploy DEPLOY_VERSION_REF is not in
        # that set. The first live run of #630 (2026-09-07, infra2 run 34107133379)
        # reported success with the pin silently skipped for exactly this reason.
        version_ref = (os.environ.get("DEPLOY_VERSION_REF") or "").strip()
        if version_ref:
            try:
                cls.pin_release(version_ref)
            except Exception as exc:  # noqa: BLE001 - every failure here must stop the deploy
                error(f"{cls.service}: could not pin release {version_ref!r}: {exc}")
                return False
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

        base.update(cls._release_recomputable_env(environment))
        base.update(
            {
                "DATA_ENGINE_IMAGE_DIGEST": image_digest,
                "RELEASE_MANIFEST_ID": release_id,
                "CAPTURE_APPROVED_BY": approved_by,
                "GIT_COMMIT_SHA": secrets.get("GIT_COMMIT_SHA") or "unknown",
            }
        )
        base["CONFIGURATION_SHA256"] = cls._configuration_sha256(base)
        return base

    @classmethod
    def _release_recomputable_env(cls, environment: str) -> dict[str, str]:
        return {
            "TA_POSTGRES_PORT": cls._POSTGRES_PORTS.get(environment, "0"),
            "TA_MINIO_S3_PORT": cls._MINIO_S3_PORTS.get(environment, "0"),
            "DAGSTER_WEBSERVER_PORT": cls._WEBSERVER_PORTS.get(environment, "0"),
            "TIER_CPU_SHARES": "512" if environment == "staging" else "1024",
            "DATA_ENGINE_MEM_LIMIT": "768m" if environment == "staging" else "1536m",
            "DATA_ENGINE_VAULT_MEM_LIMIT": "128m",
        }

    @classmethod
    def source_config_env_base(cls, env: dict | None = None) -> dict[str, str]:
        """Build infra release identity without reading TrueAlpha runtime secrets."""
        base = super().compose_env_base(env)
        base.update(cls._release_recomputable_env(base.get("ENV", "production")))
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

    #: A first pull of the ~1 GB data-engine image took longer than the original 90 s
    #: window on 2026-07-27 and again on 2026-09-04 (#595): the sync reported the OLD
    #: digest while the pull was still in flight, and the containers came up on the new
    #: one seconds later. So the verification first pulls the promoted digest itself
    #: (Docker shares layers with the pull compose already started; the call returns when
    #: the image is local), then expects the containers to switch, then waits for their
    #: healthchecks: a deploy is applied when the promoted build is RUNNING, not merely
    #: recorded.
    PULL_DEADLINE_SECONDS = 600
    SWITCH_DEADLINE_SECONDS = 180
    HEALTH_DEADLINE_SECONDS = 300
    POLL_INTERVAL_SECONDS = 5

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
            f"truealpha-dagster-code-server{suffix}",
        )

        def remote(command: str, timeout: int | None = None):
            return c.run(
                f'ssh {ssh_user}@{host} "{command}"',
                warn=True,
                hide=True,
                timeout=timeout,
            )

        # invoke raises CommandTimedOut when `timeout=` elapses instead of returning a
        # failed Result (review on #645); both shapes become the same fail-closed string.
        try:
            pulled = remote(
                f"docker pull -q {expected_image}", timeout=cls.PULL_DEADLINE_SECONDS
            )
        except CommandTimedOut as timed_out:
            pulled = None
            detail = [f"timed out after {timed_out.timeout}s"]
        else:
            detail = (pulled.stderr or pulled.stdout or "").strip().splitlines()
        if pulled is None or not pulled.ok:
            return (
                f"promoted image {expected_digest[:19]}… could not be pulled on {host} within "
                f"{cls.PULL_DEADLINE_SECONDS}s: {detail[-1] if detail else 'no output'}"
            )

        deadline = time.monotonic() + cls.SWITCH_DEADLINE_SECONDS
        last_error = "containers did not expose the promoted image"
        while True:
            mismatches = []
            for container in containers:
                result = remote(
                    f"docker inspect -f '{{{{.Config.Image}}}}' {container}"
                )
                actual = (result.stdout or "").strip()
                if not result.ok or actual != expected_image:
                    mismatches.append(f"{container}={actual or 'unavailable'}")
            if not mismatches:
                break
            last_error = ", ".join(mismatches)
            if time.monotonic() >= deadline:
                return f"promoted image digest was not applied: {last_error}"
            time.sleep(cls.POLL_INTERVAL_SECONDS)

        deadline = time.monotonic() + cls.HEALTH_DEADLINE_SECONDS
        while True:
            unhealthy = []
            for container in containers:
                result = remote(
                    f"docker inspect -f '{{{{if .State.Health}}}}{{{{.State.Health.Status}}}}"
                    f"{{{{else}}}}{{{{.State.Status}}}}{{{{end}}}}' {container}"
                )
                status = (result.stdout or "").strip()
                if not result.ok or status not in {"healthy", "running"}:
                    unhealthy.append(f"{container}={status or 'unavailable'}")
            if not unhealthy:
                return None
            if time.monotonic() >= deadline:
                return (
                    f"promoted image is running but not healthy after "
                    f"{cls.HEALTH_DEADLINE_SECONDS}s: {', '.join(unhealthy)}"
                )
            time.sleep(cls.POLL_INTERVAL_SECONDS)


if shared_tasks:
    _tasks = make_tasks(DataEngineDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
