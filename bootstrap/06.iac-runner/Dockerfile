FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY webhook_server.py .
COPY sync_runner.py .

RUN mkdir -p /workspace && \
    git config --global --add safe.directory /workspace/infra2 && \
    git config --global --add safe.directory '*'

EXPOSE 8080

CMD ["gunicorn", "-w", "1", "--threads", "4", "-b", "0.0.0.0:8080", "--timeout", "120", "webhook_server:app"]
