FROM python:3.11-slim

WORKDIR /app

# app.py is stdlib-only, but the deploy-queue-guard sidecar pulls in
# libs.dokploy (httpx + python-dotenv) and its console/task chain (rich +
# invoke) to talk to the Dokploy API. infra_probe_runner.py imports
# libs.infra_probes, which since #600 imports infra2_sdk.runtime.postgres/s3
# (psycopg/boto3-backed real protocol probes) — pin the exact same
# infra2-sdk wheel root pyproject.toml pins, by direct URL (this package is
# not published to PyPI).
RUN pip install --no-cache-dir httpx python-dotenv rich invoke \
    "psycopg[binary]>=3.2,<4" "boto3>=1.34,<2" \
    "infra2-sdk @ https://github.com/wangzitian0/infra2-sdk/releases/download/v1.0.0/infra2_sdk-1.0.0-py3-none-any.whl"

COPY platform/12.alerting/app.py /app/app.py
COPY libs /app/libs
COPY tools /app/tools

ENV PYTHONPATH=/app
EXPOSE 8080

CMD ["python", "app.py"]
