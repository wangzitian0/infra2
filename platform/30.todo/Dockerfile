FROM python:3.11-slim

WORKDIR /app

# infra2-sdk is not on PyPI: uv.lock is the only pin. tools/sdk_requirement.py reads the
# wheel URL and sha256 from the lock, and pip verifies the hash (#1115).
COPY uv.lock tools/sdk_requirement.py /tmp/sdk/
RUN sdk="$(python /tmp/sdk/sdk_requirement.py --extras fastapi,postgres,s3,http /tmp/sdk/uv.lock)" \
    && pip install --no-cache-dir \
       "uvicorn[standard]>=0.28,<1" \
       "$sdk"

COPY platform/30.todo /app/

ENV PYTHONPATH=/app
EXPOSE 8000

CMD ["python", "app.py"]
