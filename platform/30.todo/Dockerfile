FROM python:3.11-slim

WORKDIR /app

# The infra2-sdk requirement stays extras-free: libs/tests/test_sdk_contract_adoption.py
# matches the exact pyproject.toml entry. The SDK `otel` extra is listed as plain pins
# instead, and libs/tests/test_todo_canary_depth.py keeps them equal to the SDK metadata.
RUN pip install --no-cache-dir \
    httpx python-dotenv rich invoke \
    "psycopg[binary]>=3.2,<4" "boto3>=1.34,<2" \
    "opentelemetry-api>=1.24,<2" "opentelemetry-sdk>=1.24,<2" \
    "opentelemetry-exporter-otlp-proto-http>=1.24,<2" \
    "opentelemetry-instrumentation-logging>=0.45b0,<1" \
    "infra2-sdk @ https://github.com/wangzitian0/infra2-sdk/releases/download/v2.6.0/infra2_sdk-2.6.0-py3-none-any.whl"

COPY platform/30.todo /app/

ENV PYTHONPATH=/app
EXPOSE 8000

CMD ["python", "app.py"]
