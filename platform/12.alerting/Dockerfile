FROM python:3.11-slim

WORKDIR /app

# app.py is stdlib-only, but the deploy-queue-guard sidecar pulls in
# libs.dokploy (httpx + python-dotenv) and its console/task chain (rich +
# invoke) to talk to the Dokploy API. infra_probe_runner.py imports
# libs.infra_probes, which since #600 imports infra2_sdk.runtime.postgres/s3
# (psycopg/boto3-backed real protocol probes). infra2-sdk is not on PyPI:
# tools/sdk_requirement.py reads the wheel URL and sha256 from uv.lock, the only
# pin, and pip verifies the hash (#1115).
COPY uv.lock tools/sdk_requirement.py /tmp/sdk/
RUN sdk="$(python /tmp/sdk/sdk_requirement.py /tmp/sdk/uv.lock)" \
    && pip install --no-cache-dir httpx python-dotenv rich invoke \
    "psycopg[binary]>=3.2,<4" "boto3>=1.34,<2" "$sdk"

COPY platform/12.alerting/app.py /app/app.py
COPY libs /app/libs
COPY tools /app/tools

ENV PYTHONPATH=/app
EXPOSE 8080

CMD ["python", "app.py"]
