import sys

from libs.console import header, info, success, warning
from libs.deploy.deployer import Deployer, make_tasks

shared_tasks = sys.modules.get("truealpha.10.app.shared")


class AppDeployer(Deployer):
    """TrueAlpha Application Deployer (Next.js web + FastAPI llm-service)."""

    service = "app"
    compose_path = "truealpha/truealpha/10.app/compose.yaml"
    data_path = None
    secret_key = "DATABASE_URL"
    project = "truealpha"  # Dokploy project name

    # The compose owns explicit Traefik routes (truealpha[-env].<domain>), so no
    # Dokploy-managed domain here (domain routing policy: never both).
    subdomain = None
    service_port = 3000
    service_name = "web"

    @classmethod
    def pre_compose(cls, c) -> dict | None:
        env_vars = super().pre_compose(c)
        if env_vars is None:
            return None
        # Raw-archive bucket (truealpha runtime contract: immutable source
        # bytes live in S3-compatible storage; Postgres raw.fetches keeps
        # checksums + pointers). Same pattern as finance_report's bucket.
        cls._ensure_minio_bucket(c)
        return env_vars

    @classmethod
    def _ensure_minio_bucket(cls, c):
        minio_shared = sys.modules.get("platform.03.minio.shared")
        create_app_bucket = getattr(minio_shared, "create_app_bucket", None) if minio_shared else None
        if not create_app_bucket:
            warning("MinIO shared task create_app_bucket not found; skipping bucket creation")
            return

        secrets = cls.secrets()
        bucket_name = secrets.get("S3_BUCKET") or "truealpha-raw"
        existing_access_key = secrets.get("S3_ACCESS_KEY")
        existing_secret_key = secrets.get("S3_SECRET_KEY")

        if bool(existing_access_key) ^ bool(existing_secret_key):
            warning("Partial MinIO credentials found in Vault; generating a new access/secret pair")
            existing_access_key = None
            existing_secret_key = None

        if existing_access_key and existing_secret_key:
            info("MinIO credentials already exist in Vault, skipping bucket creation")
            return

        header("MinIO Bucket Setup", f"Creating raw-archive bucket: {bucket_name}")
        # lifecycle_days=0 is load-bearing: the raw archive is the append-only,
        # immutable source-of-record (point-in-time replay reads it forever) —
        # NEVER auto-expire objects, unlike report/statement buckets.
        minio_result = create_app_bucket(
            c,
            bucket_name=bucket_name,
            access_key=existing_access_key,
            secret_key=existing_secret_key,
            enable_encryption=True,
            lifecycle_days=0,
            enable_versioning=False,
            public_download=False,
        )
        if not minio_result:
            warning("MinIO bucket creation failed, please configure manually")
            return

        for key, value in (
            ("S3_ACCESS_KEY", minio_result["access_key"]),
            ("S3_SECRET_KEY", minio_result["secret_key"]),
            ("S3_BUCKET", bucket_name),
        ):
            if not secrets.get(key):
                if secrets.set(key, value):
                    success(f"Vault: {key} stored")
                else:
                    warning(f"Failed to store {key} in Vault")


if shared_tasks:
    _tasks = make_tasks(AppDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
