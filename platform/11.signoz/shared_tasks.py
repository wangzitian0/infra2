"""Shared tasks for SigNoz"""
from invoke import task
from libs.common import get_env


@task
def status(c):
    """Check SigNoz and OTEL collector health"""
    from libs.console import success, error
    
    env = get_env()
    host = env["VPS_HOST"]
    
    # Check SigNoz query service
    result = c.run(
        f"ssh root@{host} 'docker exec platform-signoz wget --spider -q localhost:8080/api/v1/health'",
        warn=True, hide=True
    )
    if result.ok:
        success("signoz: ready")
    else:
        error("signoz: not ready")
    
    # Check OTEL collector via Docker network (container has no wget/curl)
    result2 = c.run(
        f"ssh root@{host} 'docker run --rm --network=dokploy-network curlimages/curl:latest curl -s http://platform-signoz-otel-collector:13133 -o /dev/null -w \"%{{http_code}}\"'",
        warn=True, hide=True
    )
    if result2.ok and "200" in result2.stdout:
        success("otel-collector: ready")
    else:
        error("otel-collector: not ready")
    
    return {"signoz": result.ok, "otel_collector": result2.ok and "200" in result2.stdout}


@task
def test_trace(c, service_name="test"):
    """Send a test OTLP trace to verify connectivity.
    
    Usage: invoke signoz.shared.test-trace [--service-name=myapp]
    """
    from libs.console import success, error, info
    import base64
    
    env = get_env()
    host = env["VPS_HOST"]
    
    info(f"Sending test trace with service name: {service_name}")
    
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
exporter = OTLPSpanExporter(endpoint="http://platform-signoz-otel-collector:4318/v1/traces", timeout=10)
provider.add_span_processor(BatchSpanProcessor(exporter))
trace.set_tracer_provider(provider)

tracer = trace.get_tracer(__name__)
with tracer.start_as_current_span("test-parent-span") as parent:
    parent.set_attribute("test.message", "Hello from infra2 test!")
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
    
    cmd = f'ssh root@{host} \'docker run --rm --network=dokploy-network python:3.11-slim bash -c "pip install -q opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp-proto-http 2>/dev/null && echo {script_b64} | base64 -d | python3"\''
    
    result = c.run(cmd, warn=True, hide=True)
    
    if result.ok and "OK" in result.stdout:
        success("Test trace sent successfully!")
        
        # Verify in ClickHouse
        verify_query = f"SELECT count() FROM signoz_traces.distributed_signoz_index_v3 WHERE serviceName = '{service_name}'"
        verify_cmd = f'ssh root@{host} "docker exec platform-clickhouse clickhouse-client --query \\"{verify_query}\\""'
        verify_result = c.run(verify_cmd, warn=True, hide=True)
        
        if verify_result.ok and verify_result.stdout.strip():
            count = verify_result.stdout.strip()
            info(f"Traces in ClickHouse for '{service_name}': {count}")
        
        info(f"View in SigNoz UI: https://signoz.{env['INTERNAL_DOMAIN']} → Traces → service={service_name}")
        return True
    else:
        error("Failed to send test trace")
        if result.stderr:
            error(result.stderr[:500])
        return False
