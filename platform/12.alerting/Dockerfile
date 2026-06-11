FROM python:3.11-slim

WORKDIR /app

# app.py and infra_probe_runner.py are stdlib-only, but the deploy-queue-guard
# sidecar pulls in libs.dokploy (httpx + python-dotenv) and its console/task
# chain (rich + invoke). Install them so the guard can talk to the Dokploy API.
RUN pip install --no-cache-dir httpx python-dotenv rich invoke

COPY platform/12.alerting/app.py /app/app.py
COPY libs /app/libs
COPY tools /app/tools

ENV PYTHONPATH=/app
EXPOSE 8080

CMD ["python", "app.py"]
