FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ARG GIT_SHA=unknown
ARG RUNNER_INPUT_SHA256=""
LABEL git.sha="${GIT_SHA}"
LABEL runner.input.sha256="${RUNNER_INPUT_SHA256}"
ENV GIT_SHA="${GIT_SHA}"
ENV RUNNER_INPUT_SHA256="${RUNNER_INPUT_SHA256}"

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    openssh-client \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Install 1Password CLI
RUN curl -sSfLo /tmp/op.zip https://cache.agilebits.com/dist/1P/op2/pkg/v2.30.0/op_linux_amd64_v2.30.0.zip \
    && unzip -d /tmp /tmp/op.zip \
    && mv /tmp/op /usr/local/bin/op \
    && chmod +x /usr/local/bin/op \
    && rm /tmp/op.zip

# Install Vault CLI (pinned to the running server version). The vault.* invoke tasks
# (bootstrap/05.vault setup-approle) provision service credentials via
# `c.run("vault ...")`; without the CLI in this image those tasks fail their root-token
# validation and silently never provision, leaving services without VAULT_ROLE_ID /
# VAULT_SECRET_ID after a recreate.
RUN curl -sSfLo /tmp/vault.zip https://releases.hashicorp.com/vault/1.15.4/vault_1.15.4_linux_amd64.zip \
    && unzip -d /tmp /tmp/vault.zip \
    && mv /tmp/vault /usr/local/bin/vault \
    && chmod +x /usr/local/bin/vault \
    && rm /tmp/vault.zip \
    && vault version

WORKDIR /app
# Build context = repo root, so the image can read uv.lock, the only infra2-sdk pin, and
# tools/sdk_requirement.py, which turns it into a requirement whose sha256 pip verifies (#1115).
COPY bootstrap/06.iac_runner/requirements.txt .
COPY uv.lock tools/sdk_requirement.py /tmp/sdk/
RUN sdk="$(python /tmp/sdk/sdk_requirement.py /tmp/sdk/uv.lock)" \
    && pip install --no-cache-dir -r requirements.txt "$sdk"
# Each deploy task runs in a venv that `uv sync --locked` builds from the uv.lock of its
# ref, so it imports the SDK that the ref locks, not the SDK above (#1822).
RUN pip install --no-cache-dir "uv==0.9.18" && uv --version

COPY bootstrap/06.iac_runner/webhook_server.py .
COPY bootstrap/06.iac_runner/sync_runner.py .

# Fail the image build if either entrypoint imports repo-root packages absent from /app.
RUN GIT_REPO_URL=https://github.com/wangzitian0/infra2 PYTHONPATH=/app \
    python -c "import sys, webhook_server, sync_runner; assert 'common' not in sys.modules"

RUN mkdir -p /workspace && \
    git config --global --add safe.directory /workspace/infra2 && \
    git config --global --add safe.directory '*'

EXPOSE 8080

CMD ["uvicorn", "webhook_server:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--timeout-keep-alive", "95"]
