FROM python:3.11-slim

ARG GIT_SHA=unknown
LABEL git.sha="${GIT_SHA}"

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Install 1Password CLI
RUN curl -sSfLo /tmp/op.zip https://cache.agilebits.com/dist/1P/op2/pkg/v2.30.0/op_linux_amd64_v2.30.0.zip \
    && unzip -d /tmp /tmp/op.zip \
    && mv /tmp/op /usr/local/bin/op \
    && chmod +x /usr/local/bin/op \
    && rm /tmp/op.zip

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
