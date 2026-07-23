import shutil
import sys

from libs.common import get_env
from libs.console import error, header, info, success, warning
from libs.deploy.deployer import Deployer, make_tasks
from libs.env import VaultSecrets, generate_password
from libs.service_facets import SecretsFacet

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
    # TrueAlpha is an independent product with its own registered domain, not a
    # shared-platform service — override the shared INTERNAL_DOMAIN entirely so
    # its compose's ${INTERNAL_DOMAIN} Traefik Host() rules resolve under
    # truealpha.club instead of zitian.party. Read by
    # libs.app_deploy_request.make_plan via libs.service_registry.domain_for_service.
    domain = "truealpha.club"
    service_port = 3000
    service_name = "web"

    # Vault self-refresh facts (#542): the audit inventory derives from these
    # (AppRole auth from day one). Two surfaces, mirroring finance_report/app:
    #   1. the app itself;
    #   2. the multi-alias ephemeral PREVIEW surface (#522, generalized off
    #      finance_report/preview's pattern) — declared with an explicit
    #      service_id, borrowing the SOURCE env's app secrets
    #      (PREVIEW_SECRET_ENV, default staging).
    # NOTE: production rollout is _APP_COMPOSE_OVERRIDES-gated (see
    # libs/deploy_env_config.py). The prod compose_id is registered (live since
    # #547), so the vault audit covers this app AND its preview surface in
    # production — the "not yet in production" derivation dropped both
    # automatically when the compose_id landed.
    secrets = (
        SecretsFacet(
            vault_agent_container="truealpha-app-vault-agent${ENV_SUFFIX}",
            app_containers=(
                "truealpha-llm${ENV_SUFFIX}",
                "truealpha-web${ENV_SUFFIX}",
            ),
            auth_method="approle",
        ),
        SecretsFacet(
            service_id="truealpha/preview",
            compose_path="truealpha/truealpha/preview/compose.yaml",
            vault_agent_container="truealpha-app-vault-agent${ENV_SUFFIX}",
            app_containers=(
                "truealpha-llm${ENV_SUFFIX}",
                "truealpha-web${ENV_SUFFIX}",
            ),
            auth_method="approle",
        ),
    )

    @classmethod
    def compose_env_overrides(cls, *, env: str, domain: str, env_suffix: str) -> dict[str, str]:
        """Compute APP_HOST: the compose's Traefik Host() rules use this instead of
        the shared-platform-domain pattern `truealpha${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}`.

        That pattern is correct when INTERNAL_DOMAIN is the shared zitian.party domain
        (disambiguating multiple apps: truealpha.zitian.party, financereport.zitian.party,
        ...) but truealpha.club (this app's own registered domain, set via `domain` above)
        already IS "truealpha" -- prefixing it again produces the malformed
        truealpha.truealpha.club in production (empty ENV_DOMAIN_SUFFIX there collapses
        the prefix straight onto the domain with nothing to separate them; staging's
        non-empty "-staging" suffix accidentally produces a working, if redundant,
        truealpha-staging.truealpha.club). See truealpha#474.

        Fix: production is reachable at the bare domain; only non-production envs keep
        the "truealpha" prefix, preserving staging's current working hostname exactly.

        A narrow, args-only hook (not folded straight into compose_env_base's own
        DATA_PATH/ENV_SUFFIX validation dance, which is irrelevant to this one
        Traefik-routing value and produced a bogus "Nonestaging" DATA_PATH when tried,
        since AppDeployer.data_path is None) called from BOTH real deploy paths for
        this fixed app -- see compose_env_base below and its docstring for why both
        are necessary, not just one.
        """
        if env == "production":
            return {"APP_HOST": domain}
        return {"APP_HOST": f"truealpha{env_suffix}.{domain}"}

    @classmethod
    def compose_env_base(cls, env: dict | None = None) -> dict[str, str]:
        """compose_env_base is the OTHER real deploy path's hook: Deployer.composing()
        (invoke `ta-app.sync`, auto-triggered by the iac-runner's GitHub push webhook
        on every push to main touching this directory -- confirmed live, not
        hypothetical) builds its Dokploy env push from this method, and REPLACES the
        compose's entire env wholesale (only VAULT_ROLE_ID/VAULT_SECRET_ID survive via
        _preserve_runtime_env) -- omitting APP_HOST here would silently wipe it on the
        next auto-sync, reproducing truealpha#474's outage. libs.deploy.promote.deploy
        (the staging/prod deploy_v2 path) never calls this method at all -- it calls
        compose_env_overrides directly instead (see promote.py) -- so both paths must
        independently end up with APP_HOST; this just forwards to the same formula.
        """
        base = super().compose_env_base(env)
        e = env or cls.env()
        base.update(
            cls.compose_env_overrides(
                env=e.get("ENV", "production"),
                domain=base.get("INTERNAL_DOMAIN") or "",
                env_suffix=base.get("ENV_DOMAIN_SUFFIX") or "",
            )
        )
        return base

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
    def ensure_runtime_secrets(cls, c=None, *, env: str | None = None) -> bool:
        """Sync-path S3 readiness check.

        The iac-runner's sync NEVER calls pre_compose (it builds env straight
        from compose_env_base), so _ensure_minio_bucket above only runs in the
        manual pre-compose/setup tasks — a real deploy would silently ship an
        app with no S3 credentials. This hook IS called by sync; it cannot
        provision (no docker CLI in the runner) but it can refuse to be silent.

        Also called directly by libs.deploy.promote.deploy() (truealpha#447):
        that fixed-compose path never ran pre_compose/sync either, so SECRET_KEY
        was never auto-provisioned there and a fresh/rotated Vault environment
        would crash-loop app-web with no self-healing. ``env`` is required by
        that caller (see secrets_backend()) since it deploys staging and prod
        from the same process/CLI invocation.
        """
        if not super().ensure_runtime_secrets(c, env=env):
            return False
        if not cls._ensure_secret_key(env=env):
            return False
        if not cls._ensure_app_service_db_password(env=env):
            return False
        secrets = cls.secrets_backend(env=env)
        if not (secrets.get("S3_ACCESS_KEY") and secrets.get("S3_SECRET_KEY")):
            warning(
                "S3 credentials missing in Vault — the raw archive is not provisioned for this env"
            )
            info(
                "Run ONCE on the VPS host: VAULT_TOKEN=... bash "
                "truealpha/truealpha/10.app/provision_bucket.sh <staging|production>, "
                "then restart the app vault-agent."
            )
        return True

    @classmethod
    def _ensure_secret_key(cls, env: str | None = None) -> bool:
        """Auto-provision SECRET_KEY in Vault (#447).

        app-web's session JWT signing (auth/config.ts) hard-fails without this
        in production; secrets.ctmpl now renders it. Unlike the MinIO bucket
        below, generating a random signing secret needs no docker CLI, so
        (unlike _ensure_minio_bucket) this runs from the iac-runner's sync
        path too, and heals staging/production without a manual Vault step.
        """
        secrets = cls.secrets_backend(env=env)
        try:
            existing = secrets.get("SECRET_KEY")
        except VaultSecrets.VaultSecretNotFoundError:
            existing = None
        if existing:
            info("Vault secret exists: SECRET_KEY")
            return True
        if secrets.set("SECRET_KEY", generate_password(48)):
            success("Vault: SECRET_KEY generated and stored")
            return True
        error("Failed to store SECRET_KEY in Vault")
        return False

    @classmethod
    def _ensure_app_service_db_password(cls, env: str | None = None) -> bool:
        """Auto-provision app_service_login's DB password in Vault (truealpha#432
        Stage A).

        db/roles.sql creates a scoped app_service_login role (mart_readonly +
        app_runtime, no DDL) but sets no password of its own — that happens out of
        band, here, exactly like SECRET_KEY. db/apply_migrations.sh forwards this
        value to psql on every migration run, so a rotation here takes effect
        automatically on the next deploy. DATABASE_URL itself is unchanged by this —
        nothing authenticates as app_service_login yet — so a missing/rotated value
        here is inert, never a runtime outage, unlike SECRET_KEY or the S3 keys.
        """
        secrets = cls.secrets_backend(env=env)
        try:
            existing = secrets.get("APP_SERVICE_DB_PASSWORD")
        except VaultSecrets.VaultSecretNotFoundError:
            existing = None
        if existing:
            info("Vault secret exists: APP_SERVICE_DB_PASSWORD")
            return True
        if secrets.set("APP_SERVICE_DB_PASSWORD", generate_password(32)):
            success("Vault: APP_SERVICE_DB_PASSWORD generated and stored")
            return True
        error("Failed to store APP_SERVICE_DB_PASSWORD in Vault")
        return False

    @classmethod
    def _ensure_minio_bucket(cls, c):
        minio_shared = sys.modules.get("platform.03.minio.shared")
        if not minio_shared:
            warning("MinIO shared tasks module not loaded; skipping bucket creation")
            return
        create_app_bucket = getattr(minio_shared, "create_app_bucket", None)
        if not create_app_bucket:
            warning(
                "MinIO shared task create_app_bucket not found; skipping bucket creation"
            )
            return

        secrets = cls.secrets_backend()
        bucket_name = secrets.get("S3_BUCKET") or "truealpha-raw"
        existing_access_key = secrets.get("S3_ACCESS_KEY")
        existing_secret_key = secrets.get("S3_SECRET_KEY")

        if bool(existing_access_key) ^ bool(existing_secret_key):
            warning(
                "Partial MinIO credentials found in Vault; generating a new access/secret pair"
            )
            existing_access_key = None
            existing_secret_key = None

        if existing_access_key and existing_secret_key:
            info("MinIO credentials already exist in Vault, skipping bucket creation")
            # The never-expire invariant must hold for pre-existing buckets too.
            cls._ensure_never_expires(c, bucket_name)
            return

        if shutil.which("docker") is None:
            # The iac-runner deploys through the Dokploy API and has no docker
            # CLI/socket, so create_app_bucket (docker exec ... mc) can never
            # work from a deploy — that silent degradation is how the first
            # truealpha staging deploys "succeeded" with no bucket. Point at
            # the host-side path instead of warning vaguely.
            warning(
                f"docker CLI unavailable — cannot provision bucket '{bucket_name}' from this deploy"
            )
            info(
                "Run ONCE on the VPS host: VAULT_TOKEN=... bash "
                "truealpha/truealpha/10.app/provision_bucket.sh <staging|production>, "
                "then restart the app vault-agent."
            )
            return

        header("MinIO Bucket Setup", f"Creating raw-archive bucket: {bucket_name}")
        # lifecycle_days=0 means create_app_bucket ADDS no expiry rule — the raw
        # archive is the append-only, immutable source-of-record (point-in-time
        # replay reads it forever), unlike report/statement buckets.
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
        cls._ensure_never_expires(c, bucket_name)

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

    @classmethod
    def _ensure_never_expires(cls, c, bucket_name):
        """Remove ALL lifecycle rules from the raw-archive bucket.

        create_app_bucket(lifecycle_days=0) only refrains from ADDING an expiry
        rule — it never removes one, so a bucket that predates this deploy with
        an ILM policy attached would silently keep deleting raw objects. The
        raw archive must never expire; clearing the rules makes that true
        regardless of the bucket's history."""
        if shutil.which("docker") is None:
            info(
                "docker CLI unavailable — lifecycle check runs host-side via provision_bucket.sh"
            )
            return
        env_suffix = get_env().get("ENV_SUFFIX", "")
        container = f"platform-minio{env_suffix}"
        result = c.run(
            f"docker exec {container} mc ilm rm --all --force local/{bucket_name}",
            hide=True,
            warn=True,
        )
        output = f"{result.stdout or ''} {result.stderr or ''}".lower()
        if result.ok:
            success(
                f"Lifecycle rules cleared on '{bucket_name}' — raw archive never expires"
            )
        elif "does not exist" in output or "no lifecycle" in output:
            info(f"No lifecycle rules on '{bucket_name}' — raw archive never expires")
        else:
            warning(
                f"Could not clear lifecycle rules on '{bucket_name}', verify manually: {result.stderr}"
            )


if shared_tasks:
    _tasks = make_tasks(AppDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
