"""Shared tasks for SigNoz"""

import re
import json
from invoke import task
from libs.common import check_service, get_env, service_domain, with_env_suffix

CLICKHOUSE_INGESTION_DELAY_SECONDS = 2
DEFAULT_API_KEY_NAME = "infra-automation"
DEFAULT_API_KEY_EXPIRY_DAYS = 365


def _sanitize_service_name(name: str) -> str:
    """Sanitize service name to prevent SQL injection."""
    # Only allow alphanumeric, hyphen, underscore
    if not re.match(r"^[a-zA-Z0-9_-]+$", name):
        raise ValueError(
            f"Invalid service name: {name}. Only alphanumeric, hyphen, underscore allowed."
        )
    return name


@task
def status(c):
    """Check SigNoz and OTEL collector health."""
    from libs.console import success, error

    env = get_env()
    host = env["VPS_HOST"]
    signoz_result = check_service(
        c, "signoz", "wget --spider -q localhost:8080/api/v1/health"
    )

    # Check OTEL collector via Docker network (container has no wget/curl)
    collector = with_env_suffix("platform-signoz-otel-collector", env)
    result2 = c.run(
        f"ssh root@{host} 'docker run --rm --network=dokploy-network curlimages/curl:latest curl -s http://{collector}:13133 -o /dev/null -w \"%{{http_code}}\"'",
        warn=True,
        hide=True,
    )
    if result2.ok and "200" in result2.stdout:
        success("otel_collector: ready")
    else:
        error("otel_collector: not ready")

    return {
        "signoz": signoz_result.get("is_ready"),
        "otel_collector": result2.ok and "200" in result2.stdout,
    }


@task
def test_trace(c, service_name="test"):
    """Send a test OTLP trace to verify connectivity.

    Usage:
        invoke signoz.shared.test-trace
        invoke signoz.shared.test_trace --service-name=myapp
    """
    from libs.console import success, error, info
    import base64
    import time

    # Validate service name to prevent injection
    service_name = _sanitize_service_name(service_name)

    env = get_env()
    host = env["VPS_HOST"]

    info(f"Sending test trace with service name: {service_name}")

    collector = with_env_suffix("platform-signoz-otel-collector", env)
    # Python script to send test trace
    python_script = f'''
import time
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource

resource = Resource.create({{"service.name": "{service_name}", "deployment.environment": "test"}})
provider = TracerProvider(resource=resource)
exporter = OTLPSpanExporter(endpoint="http://{collector}:4318/v1/traces", timeout=10)
provider.add_span_processor(BatchSpanProcessor(exporter))
trace.set_tracer_provider(provider)

tracer = trace.get_tracer(__name__)
with tracer.start_as_current_span("test-parent-span") as parent:
    parent.set_attribute("test.message", "OTLP connectivity test")
    time.sleep(0.1)
    with tracer.start_as_current_span("test-child-span") as child:
        child.set_attribute("test.step", "child operation")
        time.sleep(0.05)

provider.force_flush()
provider.shutdown()
print("OK")
'''

    # Base64 encode to avoid quoting issues
    script_b64 = base64.b64encode(python_script.encode()).decode()

    # Use pip cache volume to speed up repeated runs
    cmd = f"ssh root@{host} 'docker run --rm --network=dokploy-network -v signoz-otel-pip-cache:/root/.cache/pip python:3.11-slim bash -c \"pip install -q opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp-proto-http 2>/dev/null && echo {script_b64} | base64 -d | python3\"'"

    result = c.run(cmd, warn=True, hide=True)

    if result.ok and "OK" in result.stdout:
        success("Test trace sent successfully!")

        # Wait for ClickHouse ingestion
        time.sleep(CLICKHOUSE_INGESTION_DELAY_SECONDS)

        # Verify in ClickHouse (service_name already validated)
        verify_query = f"SELECT count() FROM signoz_traces.distributed_signoz_index_v3 WHERE serviceName = '{service_name}'"
        clickhouse = with_env_suffix("platform-clickhouse", env)
        verify_cmd = f'ssh root@{host} "docker exec {clickhouse} clickhouse-client --query \\"{verify_query}\\""'
        verify_result = c.run(verify_cmd, warn=True, hide=True)

        if verify_result.ok and verify_result.stdout.strip():
            count = verify_result.stdout.strip()
            info(f"Traces in ClickHouse for '{service_name}': {count}")

        domain = service_domain("signoz", env)
        if domain:
            info(
                f"View in SigNoz UI: https://{domain} → Traces → service={service_name}"
            )
        return True
    else:
        error("Failed to send test trace")
        if result.stderr:
            error(f"stderr: {result.stderr[:500]}")
        if result.stdout:
            error(f"stdout: {result.stdout[:500]}")
        return False


@task
def create_api_key(
    c,
    name=DEFAULT_API_KEY_NAME,
    expiry_days=DEFAULT_API_KEY_EXPIRY_DAYS,
    store_vault=True,
):
    """Create SigNoz API key and optionally store in Vault.

    Requires admin credentials from 1Password: platform/signoz/admin

    Usage:
        invoke signoz.shared.create-api-key
        invoke signoz.shared.create-api-key --name=custom-key --expiry-days=30
        invoke signoz.shared.create-api-key --no-store-vault
    """
    from libs.console import success, error, info, warning
    import subprocess

    env = get_env()
    domain = service_domain("signoz", env)
    if not domain:
        error("Cannot determine SigNoz domain")
        return None

    base_url = f"https://{domain}"

    op_item = "platform/signoz/admin"
    try:
        email_result = subprocess.run(
            ["op", "read", f"op://Infra2/{op_item}/username"],
            capture_output=True,
            text=True,
            check=True,
        )
        password_result = subprocess.run(
            ["op", "read", f"op://Infra2/{op_item}/password"],
            capture_output=True,
            text=True,
            check=True,
        )
        admin_email = email_result.stdout.strip()
        admin_password = password_result.stdout.strip()
    except subprocess.CalledProcessError as e:
        error(f"Failed to read credentials from 1Password: {op_item}", str(e))
        return None

    info(f"Logging in to SigNoz as {admin_email}")
    login_result = c.run(
        f'curl -s -X POST "{base_url}/api/v1/login" '
        f'-H "Content-Type: application/json" '
        f'-d \'{{"email": "{admin_email}", "password": "{admin_password}"}}\'',
        hide=True,
        warn=True,
    )

    if not login_result.ok:
        error("Failed to login to SigNoz")
        return None

    try:
        login_data = json.loads(login_result.stdout)
        jwt_token = login_data.get("data", {}).get("accessJwt")
        if not jwt_token:
            error("No JWT token in login response")
            return None
    except json.JSONDecodeError:
        error("Invalid JSON response from login")
        return None

    info(f"Creating API key: {name} (expires in {expiry_days} days)")
    create_result = c.run(
        f'curl -s -X POST "{base_url}/api/v1/pats" '
        f'-H "Authorization: Bearer {jwt_token}" '
        f'-H "Content-Type: application/json" '
        f'-d \'{{"name": "{name}", "expiresInDays": {expiry_days}, "role": "ADMIN"}}\'',
        hide=True,
        warn=True,
    )

    if not create_result.ok:
        error("Failed to create API key")
        return None

    try:
        api_key_data = json.loads(create_result.stdout)
        if api_key_data.get("status") != "success":
            error("API key creation failed", api_key_data.get("error", "Unknown error"))
            return None

        key_info = api_key_data.get("data", {})
        api_key = key_info.get("token")
        api_key_id = key_info.get("id")

        if not api_key:
            error("No token in API key response")
            return None

    except json.JSONDecodeError:
        error("Invalid JSON response from API key creation")
        return None

    success(f"Created API key: {name} (id: {api_key_id})")

    if store_vault:
        from libs.env import get_secrets

        deploy_env = env.get("DEPLOY_ENV") or "production"
        secrets = get_secrets("platform", "signoz", deploy_env)

        vault_data = {
            "api_key": api_key,
            "api_key_name": name,
            "api_key_id": api_key_id,
            "url": base_url,
        }

        for k, v in vault_data.items():
            if secrets.set(k, v):
                info(f"Stored {k} in Vault")
            else:
                warning(f"Failed to store {k} in Vault")

        success(f"API key stored in Vault: secret/platform/{deploy_env}/signoz")
    else:
        info(f"API key (not stored): {api_key}")

    return {
        "api_key": api_key,
        "api_key_id": api_key_id,
        "api_key_name": name,
        "url": base_url,
    }
