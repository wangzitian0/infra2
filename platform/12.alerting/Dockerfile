FROM python:3.11-slim

WORKDIR /app

# infra2-sdk is not on PyPI: uv.lock is the only pin. tools/sdk_requirement.py reads the
# wheel URL and sha256 from the lock, and pip verifies the hash (#1115).
COPY uv.lock tools/sdk_requirement.py /tmp/sdk/
RUN sdk="$(python /tmp/sdk/sdk_requirement.py /tmp/sdk/uv.lock)" \
    && pip install --no-cache-dir httpx python-dotenv rich invoke \
       "psycopg[binary]>=3.2,<4" "boto3>=1.34,<2" \
       "fastapi>=0.110,<1" "uvicorn[standard]>=0.28,<1" \
       "$sdk"

COPY platform/12.alerting/app.py /app/app.py
COPY libs /app/libs
COPY tools /app/tools

ENV PYTHONPATH=/app
EXPOSE 8080

CMD ["python", "app.py"]
