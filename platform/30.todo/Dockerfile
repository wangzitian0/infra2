FROM python:3.11-slim

WORKDIR /app

# infra2-sdk is not on PyPI: uv.lock is the only pin. Python reads the wheel URL
# and sha256 from the lock, and pip verifies the hash (#1115).
COPY uv.lock /tmp/uv.lock
RUN sdk="$(python -c "import tomllib, pathlib; doc = tomllib.loads(pathlib.Path('/tmp/uv.lock').read_text()); w = next(p for p in doc['package'] if p['name'] == 'infra2-sdk')['wheels'][0]; url = w['url']; h = w['hash'].replace(':', '='); print(f'infra2-sdk @ {url}#{h}')")" \
    && pip install --no-cache-dir \
       httpx python-dotenv rich invoke \
       "psycopg[binary]>=3.2,<4" "boto3>=1.34,<2" \
       "opentelemetry-api>=1.24,<2" "opentelemetry-sdk>=1.24,<2" \
       "opentelemetry-exporter-otlp-proto-http>=1.24,<2" \
       "opentelemetry-instrumentation-logging>=0.45b0,<1" \
       "$sdk"

COPY platform/30.todo /app/

ENV PYTHONPATH=/app
EXPOSE 8000

CMD ["python", "app.py"]
