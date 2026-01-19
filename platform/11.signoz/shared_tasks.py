"""Shared tasks for SigNoz"""
import json
import re
import secrets
import string
import subprocess

import httpx
from invoke import task

from libs.common import check_service, get_env, service_domain, with_env_suffix
from libs.console import error, info, success, warning

# Delay before querying ClickHouse to allow trace ingestion
CLICKHOUSE_INGESTION_DELAY_SECONDS = 2
ADMIN_OP_ITEM = "platform/signoz/admin"
ADMIN_NAME = "SigNoz Admin"
ADMIN_EMAIL_PREFIX = "signoz-admin"
PASSWORD_SYMBOLS = "!@#$%^&*()-_=+[]{}:,.?"
PASSWORD_MIN_LENGTH = 16


def _sanitize_service_name(name: str) -> str:
    """Sanitize service name to prevent SQL injection."""
    # Only allow alphanumeric, hyphen, underscore
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        raise ValueError(f"Invalid service name: {name}. Only alphanumeric, hyphen, underscore allowed.")
    return name


def _admin_item_name(env: dict) -> str:
    env_name = env.get("ENV", "production")
    if env_name != "production":
        return f"{ADMIN_OP_ITEM}-{env_name}"
    return ADMIN_OP_ITEM


def _default_admin_email(env: dict) -> str:
    domain = env.get("INTERNAL_DOMAIN") or "example.com"
    return f"{ADMIN_EMAIL_PREFIX}@{domain}"


def _generate_admin_password(length: int = PASSWORD_MIN_LENGTH) -> str:
    if length < 12:
        raise ValueError("Admin password length must be at least 12 characters.")
    alphabet = string.ascii_letters + string.digits + PASSWORD_SYMBOLS
    required = [
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
        secrets.choice(PASSWORD_SYMBOLS),
    ]
    while True:
        remaining = [secrets.choice(alphabet) for _ in range(length - len(required))]
        password = required + remaining
        secrets.SystemRandom().shuffle(password)
        candidate = "".join(password)
        if (
            any(ch.islower() for ch in candidate)
            and any(ch.isupper() for ch in candidate)
            and any(ch.isdigit() for ch in candidate)
            and any(ch in PASSWORD_SYMBOLS for ch in candidate)
        ):
            return candidate


def _read_admin_from_1password(item: str) -> tuple[str, str] | None:
    try:
        result = subprocess.run(
            ["op", "item", "get", item, "--vault=Infra2", "--format=json"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        warning(f"1Password read failed for {item}: {exc}")
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        warning(f"1Password JSON parse failed for {item}: {exc}")
        return None

    username = None
    password = None
    for field in data.get("fields", []):
        field_id = field.get("id")
        purpose = field.get("purpose")
        label = (field.get("label") or "").lower()
        if field_id == "username" or purpose == "USERNAME" or label == "username":
            username = field.get("value")
        if field_id == "password" or purpose == "PASSWORD" or label == "password":
            password = field.get("value")

    if username and password:
        return username, password
    return None


def _sync_admin_to_1password(item: str, username: str, password: str, url: str) -> bool:
    try:
        check_result = subprocess.run(
            ["op", "item", "get", item, "--vault=Infra2", "--format=json"],
            capture_output=True,
            text=True,
        )
        if check_result.returncode == 0:
            subprocess.run(
                [
                    "op",
                    "item",
                    "edit",
                    item,
                    "--vault=Infra2",
                    f"username={username}",
                    f"password={password}",
                    f"url={url}",
                ],
                capture_output=True,
                check=True,
            )
            info("1Password: Updated existing admin item")
        else:
            subprocess.run(
                [
                    "op",
                    "item",
                    "create",
                    "--category=login",
                    f"--title={item}",
                    "--vault=Infra2",
                    f"username={username}",
                    f"password={password}",
                    f"url={url}",
                ],
                capture_output=True,
                check=True,
            )
            success("1Password: Created admin item")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        warning(f"1Password sync failed for {item}: {exc}")
        return False


def _register_admin(base_url: str, email: str, password: str) -> tuple[str, str]:
    url = f"{base_url}/api/v1/register"
    payload = {"name": ADMIN_NAME, "email": email, "password": password}
    try:
        response = httpx.post(url, json=payload, timeout=10.0)
    except httpx.RequestError as exc:
        return "failed", f"request error: {exc}"

    try:
        data = response.json()
    except ValueError:
        return "failed", f"unexpected response: HTTP {response.status_code}"

    if data.get("status") == "success":
        return "created", "admin created"

    error_info = data.get("error", {})
    message = error_info.get("message", "") or "registration failed"
    normalized = message.lower()
    if "self-registration is disabled" in normalized or "self registration is disabled" in normalized:
        return "disabled", message
    if "already exists" in normalized:
        return "exists", message
    return "failed", message


@task
def status(c):
    """Check SigNoz and OTEL collector health."""
    env = get_env()
    host = env["VPS_HOST"]
    signoz_result = check_service(c, "signoz", "wget --spider -q localhost:8080/api/v1/health")

    # Check OTEL collector via Docker network (container has no wget/curl)
    collector = with_env_suffix("platform-signoz-otel-collector", env)
    result2 = c.run(
        f"ssh root@{host} 'docker run --rm --network=dokploy-network curlimages/curl:latest curl -s http://{collector}:13133 -o /dev/null -w \"%{{http_code}}\"'",
        warn=True, hide=True
    )
    if result2.ok and "200" in result2.stdout:
        success("otel_collector: ready")
    else:
        error("otel_collector: not ready")

    return {"signoz": signoz_result.get("is_ready"), "otel_collector": result2.ok and "200" in result2.stdout}


@task
def ensure_admin(c, email: str | None = None):
    """Ensure SigNoz admin credentials exist and are stored in 1Password."""
    env = get_env()
    domain = service_domain("signoz", env)
    if not domain:
        error("INTERNAL_DOMAIN not set; cannot resolve SigNoz domain")
        return False

    base_url = f"https://{domain}"
    op_item = _admin_item_name(env)
    existing = _read_admin_from_1password(op_item)
    if existing:
        info(f"1Password admin credentials already exist: {op_item}")
        return True

    admin_email = email or _default_admin_email(env)
    admin_password = _generate_admin_password()
    status, detail = _register_admin(base_url, admin_email, admin_password)

    if status == "created":
        if _sync_admin_to_1password(op_item, admin_email, admin_password, base_url):
            success("SigNoz admin created and stored in 1Password")
            return True
        warning("SigNoz admin created, but 1Password sync failed")
        return False

    if status in {"disabled", "exists"}:
        warning(f"SigNoz registration blocked: {detail}")
        warning(f"Create or reset admin manually, then store in 1Password: {op_item}")
        return False

    warning(f"SigNoz admin creation failed: {detail}")
    return False


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
    cmd = f'ssh root@{host} \'docker run --rm --network=dokploy-network -v signoz-otel-pip-cache:/root/.cache/pip python:3.11-slim bash -c "pip install -q opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp-proto-http 2>/dev/null && echo {script_b64} | base64 -d | python3"\''
    
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
            info(f"View in SigNoz UI: https://{domain} → Traces → service={service_name}")
        return True
    else:
        error("Failed to send test trace")
        if result.stderr:
            error(f"stderr: {result.stderr[:500]}")
        if result.stdout:
            error(f"stdout: {result.stdout[:500]}")
        return False
