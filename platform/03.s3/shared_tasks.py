import io
import json
import shlex

from invoke import task

from libs.common import check_service, get_env, service_domain
from libs.security.store import generate_password
from libs.console import header, success, error, warning, info


@task
def status(c):
    """Check S3 Object Storage status: container health + both endpoints."""
    # Check container health via docker (try platform-s3 then fallback to platform-minio)
    result = check_service(c, "s3", "mc ready local")
    if not result.get("is_ready"):
        result = check_service(c, "minio", "mc ready local")

    if not result.get("is_ready"):
        return result

    # Also verify external endpoints are reachable
    e = get_env()
    console_host = service_domain("minio", e)
    api_host = service_domain("s3", e)
    if console_host and api_host:
        endpoints = [
            (f"https://{console_host}", "Console"),
            (f"https://{api_host}", "S3 API"),
        ]
        info(f"Checking external endpoints for domain: {e.get('INTERNAL_DOMAIN')}")
        for url, name in endpoints:
            check = c.run(
                f"curl -sI {url} -o /dev/null -w '%{{http_code}}'", hide=True, warn=True
            )
            code = check.stdout.strip() if check.ok else "error"

            if code == "200":
                success(f"   {name} ({url}): HTTP {code} (OK)")
            elif (
                code == "403" or code == "400"
            ):  # 400/403 is OK for S3 API without auth
                success(f"   {name} ({url}): HTTP {code} (API Active)")
            else:
                warning(f"   {name} ({url}): HTTP {code}")
                result["details"] += f"; {name}: HTTP {code}"

    return result


def _ensure_admin_alias(c, container_name: str, e: dict) -> bool:
    """Point the container's ``local`` alias at this instance's root credential.

    The image ships an alias with an empty access key, so every ``mc admin`` call in this
    file answered "Access Denied" — which is how three applications ended up configured
    with the MinIO root credential by hand instead (#677). ``mc alias import`` reads the
    credential as JSON on stdin, so it appears in no command line: not in this host's
    process list, and not in the container's.
    """
    from libs.security.store import get_secrets

    env_name = e.get("ENV", "production")
    s3_secrets = None
    try:
        s3_secrets = get_secrets("platform", "s3", env_name)
    except Exception:
        s3_secrets = None
    if not s3_secrets or not s3_secrets.get("root_password"):
        try:
            s3_secrets = get_secrets("platform", "minio", env_name)
        except Exception as exc:  # noqa: BLE001 - a missing store is a clear operator error
            error(f"Could not read the S3 root credential from Vault: {exc}")
            return False
    root_user = (s3_secrets.get("root_user") if s3_secrets else None) or "admin"
    root_password = s3_secrets.get("root_password") if s3_secrets else None
    if not root_password:
        error(f"platform/{env_name}/s3 (or minio) holds no root_password")
        return False
    payload = json.dumps(
        {
            "url": "http://127.0.0.1:9000",
            "accessKey": root_user,
            "secretKey": root_password,
            "api": "s3v4",
            "path": "auto",
        }
    )
    result = c.run(
        f"docker exec -i {container_name} mc alias import local /dev/stdin",
        in_stream=io.StringIO(payload),
        hide=True,
        warn=True,
    )
    if not result.ok:
        error("Failed to configure the S3 admin alias")
        return False
    return True


def _resolve_s3_container(c, env_suffix: str) -> str:
    container_name = f"platform-s3{env_suffix}"
    probe = c.run(
        f"docker inspect --format '{{{{.State.Running}}}}' {container_name}",
        hide=True,
        warn=True,
    )
    if not probe.ok or probe.stdout.strip() != "true":
        container_name = f"platform-minio{env_suffix}"
    return container_name


def _create_bucket_resource(c, container_name: str, bucket_name: str) -> bool:
    info(f"Creating bucket '{bucket_name}'...")
    result = c.run(
        f"docker exec {container_name} mc mb local/{bucket_name} --ignore-existing",
        hide=True,
        warn=True,
    )
    if result.ok:
        success(f"Bucket '{bucket_name}' ready")
        return True
    error(f"Failed to create bucket: {result.stderr}")
    return False


def _apply_bucket_policies(
    c,
    container_name: str,
    bucket_name: str,
    public_download: bool,
    enable_encryption: bool,
    lifecycle_days: int,
    enable_versioning: bool,
) -> bool:
    if public_download:
        info(
            "Setting bucket policy: public anonymous download (direct object URL access)..."
        )
        result = c.run(
            f"docker exec {container_name} mc anonymous set download local/{bucket_name}",
            hide=True,
            warn=True,
        )
        if result.ok:
            success("Public anonymous download enabled (direct object URL access)")
        else:
            warning(f"Failed to set public policy: {result.stderr}")
    else:
        info("Ensuring bucket policy: private (no anonymous direct object access)...")
        result = c.run(
            f"docker exec {container_name} mc anonymous set none local/{bucket_name}",
            hide=True,
            warn=True,
        )
        if result.ok:
            success("Anonymous download disabled")
        else:
            warning(f"Failed to disable anonymous policy: {result.stderr}")

    if enable_encryption:
        info("Enabling server-side encryption (SSE-S3)...")
        result = c.run(
            f"docker exec {container_name} mc encrypt set sse-s3 local/{bucket_name}",
            hide=True,
            warn=True,
        )
        if not result.ok:
            error(f"Failed to enable encryption: {result.stderr}")
            return False

        probe_key = f".probe-encryption-{bucket_name}"
        probe_put = c.run(
            f"docker exec {container_name} sh -c 'printf \"test\" | mc pipe local/{bucket_name}/{probe_key}'",
            hide=True,
            warn=True,
        )
        if not probe_put.ok:
            c.run(
                f"docker exec {container_name} mc encrypt clear local/{bucket_name}",
                hide=True,
                warn=True,
            )
            error(
                f"SSE-S3 enabled but write probe failed (backend lacks KMS support): {probe_put.stderr}"
            )
            return False

        c.run(
            f"docker exec {container_name} mc rm --force local/{bucket_name}/{probe_key}",
            hide=True,
            warn=True,
        )
        success("Server-side encryption enabled and verified (SSE-S3)")

    if lifecycle_days > 0:
        info(f"Setting lifecycle policy: auto-delete after {lifecycle_days} days...")
        result = c.run(
            f"docker exec {container_name} mc ilm add local/{bucket_name} "
            f"--expiry-days {lifecycle_days}",
            hide=True,
            warn=True,
        )
        if result.ok:
            success(f"Lifecycle policy: files deleted after {lifecycle_days} days")
        else:
            warning(f"Failed to set lifecycle: {result.stderr}")

    if enable_versioning:
        info("Enabling bucket versioning...")
        result = c.run(
            f"docker exec {container_name} mc version enable local/{bucket_name}",
            hide=True,
            warn=True,
        )
        if result.ok:
            success("Bucket versioning enabled")
        else:
            warning(f"Failed to enable versioning: {result.stderr}")

    return True


def _provision_bucket_service_account(
    c, container_name: str, bucket_name: str, access_key: str, secret_key: str
) -> bool:
    info(f"Creating S3 service account: {access_key}...")
    result = c.run(
        f"docker exec {container_name} mc admin user add local {access_key} {secret_key}",
        hide=True,
        warn=True,
    )
    if result.ok:
        success(f"Service account created: {access_key}")
    else:
        warning("User may already exist, attempting to update...")
        c.run(
            f"docker exec {container_name} mc admin user remove local {access_key}",
            hide=True,
            warn=True,
        )
        result = c.run(
            f"docker exec {container_name} mc admin user add local {access_key} {secret_key}",
            hide=True,
            warn=True,
        )
        if result.ok:
            success(f"Service account updated: {access_key}")
        else:
            error(f"Failed to create/update user: {result.stderr}")
            return False

    safe_bucket_name = "".join(char if char.isalnum() else "_" for char in bucket_name)
    policy_name = f"{safe_bucket_name}_readwrite"
    policy_doc = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetBucketLocation",
                    "s3:ListBucket",
                ],
                "Resource": [f"arn:aws:s3:::{bucket_name}"],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "s3:DeleteObject",
                    "s3:GetObject",
                    "s3:PutObject",
                ],
                "Resource": [f"arn:aws:s3:::{bucket_name}/*"],
            },
        ],
    }
    policy_json = json.dumps(policy_doc)
    policy_path = f"/tmp/{policy_name}.json"
    write_policy_script = f"cat > {policy_path} <<'EOF'\n{policy_json}\nEOF"
    info(f"Creating bucket-scoped policy: {policy_name}...")
    result = c.run(
        f"docker exec {container_name} sh -c {shlex.quote(write_policy_script)}",
        hide=True,
        warn=True,
    )
    if result.ok:
        result = c.run(
            f"docker exec {container_name} mc admin policy create local {policy_name} {policy_path}",
            hide=True,
            warn=True,
        )
        if result.ok:
            success(f"Policy ready: {policy_name}")
        else:
            warning(f"Failed to create policy {policy_name}: {result.stderr}")
    else:
        warning(f"Failed to write policy file: {result.stderr}")

    info(f"Attaching bucket-scoped policy to {access_key}...")
    result = c.run(
        f"docker exec {container_name} mc admin policy attach local {policy_name} "
        f"--user {access_key}",
        hide=True,
        warn=True,
    )
    if result.ok:
        success(f"Policy attached: {access_key} -> {policy_name}")
    else:
        warning(f"Failed to attach policy: {result.stderr}")

    return True


def _verify_bucket_setup(
    c,
    container_name: str,
    bucket_name: str,
    enable_encryption: bool,
    lifecycle_days: int,
) -> bool:
    header("Verification", f"Bucket '{bucket_name}' configuration")
    verification_ok = True

    result = c.run(
        f"docker exec {container_name} mc ls local/{bucket_name}", hide=True, warn=True
    )
    if not result.ok:
        warning(f"Bucket verification failed: unable to list '{bucket_name}'.")
        verification_ok = False

    result = c.run(
        f"docker exec {container_name} mc anonymous get local/{bucket_name}",
        hide=True,
        warn=True,
    )
    if not result.ok:
        warning(
            f"Bucket verification failed: unable to read anonymous policy for '{bucket_name}'."
        )
        verification_ok = False

    if enable_encryption:
        result = c.run(
            f"docker exec {container_name} mc encrypt info local/{bucket_name}",
            hide=True,
            warn=True,
        )
        if not result.ok:
            warning(
                f"Bucket verification failed: unable to read encryption info for '{bucket_name}'."
            )
            verification_ok = False

    if lifecycle_days > 0:
        result = c.run(
            f"docker exec {container_name} mc ilm ls local/{bucket_name}",
            hide=True,
            warn=True,
        )
        if not result.ok:
            warning(
                f"Bucket verification failed: unable to read lifecycle rules for '{bucket_name}'."
            )
            verification_ok = False

    if verification_ok:
        success("Bucket setup complete!")
    else:
        warning("Bucket setup completed with verification warnings")
    return verification_ok


@task
def create_app_bucket(
    c,
    bucket_name,
    access_key=None,
    secret_key=None,
    enable_encryption=False,
    lifecycle_days=90,
    enable_versioning=False,
    public_download=False,
):
    """Create S3 bucket with security best practices for application usage."""
    header("S3 Bucket Setup", f"Creating bucket: {bucket_name}")

    e = get_env()
    env_suffix = e.get("ENV_SUFFIX", "")
    container_name = _resolve_s3_container(c, env_suffix)

    if not access_key:
        access_key = bucket_name.replace("-", "_")
        info(f"Generated access_key: {access_key}")

    if not secret_key:
        secret_key = generate_password(32)
        info("Generated secret_key: <hidden>")

    if not _ensure_admin_alias(c, container_name, e):
        return None

    if not _create_bucket_resource(c, container_name, bucket_name):
        return None

    if not _apply_bucket_policies(
        c,
        container_name,
        bucket_name,
        public_download,
        enable_encryption,
        lifecycle_days,
        enable_versioning,
    ):
        return None

    if not _provision_bucket_service_account(
        c, container_name, bucket_name, access_key, secret_key
    ):
        return None

    _verify_bucket_setup(
        c, container_name, bucket_name, enable_encryption, lifecycle_days
    )

    info("Next steps:")
    info("  1. Store credentials in Vault (project/env/service secrets)")
    info(f"     - S3_ACCESS_KEY={access_key}")
    info("     - S3_SECRET_KEY=<hidden>")
    info(f"     - S3_BUCKET={bucket_name}")

    return {
        "access_key": access_key,
        "secret_key": secret_key,
        "bucket": bucket_name,
    }
