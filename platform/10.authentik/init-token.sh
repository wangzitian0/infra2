#!/bin/sh
# Authentik Root Token initialization script
# Creates admin API token and stores as 'root_token' in Vault

set -e

# Prefer python, fall back to python3 for minimal images.
PYTHON_BIN=python
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN=python3
  else
    echo "Error: python not found in container"
    exit 1
  fi
fi

# Wait for Authentik server to be ready (token-init container has no local API)
AUTHENTIK_SERVER_HOST="platform-authentik-server${ENV_SUFFIX}:9000"
export AUTHENTIK_SERVER_HOST
echo "Waiting for Authentik server to be ready..."
until "$PYTHON_BIN" - <<'PY'
import os
import sys
import urllib.request

host = os.environ.get("AUTHENTIK_SERVER_HOST")
if not host:
    sys.exit(1)

url = f"http://{host}/-/health/live/"
try:
    with urllib.request.urlopen(url, timeout=3) as resp:
        sys.exit(0 if resp.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
do
  sleep 5
done

echo "Authentik is ready, checking for root token..."

# Create API token using Django shell and capture output
TOKEN=$("$PYTHON_BIN" -m manage shell << 'PYTHON'
from authentik.core.models import User, Token, TokenIntents
from datetime import datetime, timedelta

try:
    user = User.objects.get(username='akadmin')
    
    # Check if token already exists
    token = Token.objects.filter(
        identifier='root-automation',
        user=user,
        intent=TokenIntents.INTENT_API
    ).first()
    
    if token:
        # Token exists, output only the key (no logging for security)
        print(token.key, end='')
    else:
        # Create new token with 10-year expiry
        token = Token.objects.create(
            identifier='root-automation',
            user=user,
            intent=TokenIntents.INTENT_API,
            expiring=True,
            expires=datetime.now() + timedelta(days=3650)
        )
        # Output only the key (no logging for security)
        print(token.key, end='')
except Exception as e:
    print(f"Error: {e}", file=__import__('sys').stderr)
    exit(1)
PYTHON
)
PYTHON_EXIT=$?

if [ $PYTHON_EXIT -ne 0 ]; then
  echo "Error: Python script failed"
  exit 1
fi

if [ -z "$TOKEN" ]; then
  echo "Error: Failed to create or retrieve token"
  exit 1
fi

echo "Root token initialized successfully"

# Store token to Vault if VAULT_INIT_TOKEN is set
if [ -n "$VAULT_INIT_TOKEN" ]; then
  echo "Storing root token to Vault..."
  
  # Use VAULT_INIT_ADDR directly (passed from compose.yaml)
  VAULT_URL="${VAULT_INIT_ADDR:-https://vault.zitian.party}"
  ENV_NAME="${ENV:-production}"
  
  # Use Python urllib to call Vault HTTP API (curl not available in container)
  "$PYTHON_BIN" - << PYTHON
import os
import sys
import json
import urllib.request

vault_url = "${VAULT_URL}/v1/secret/data/platform/${ENV_NAME}/authentik"
token = """${TOKEN}"""
vault_token = os.environ.get("VAULT_INIT_TOKEN")

data = json.dumps({"data": {"root_token": token}}).encode('utf-8')
req = urllib.request.Request(
    vault_url,
    data=data,
    headers={
        "X-Vault-Token": vault_token,
        "Content-Type": "application/json"
    },
    method="PATCH"
)

try:
    with urllib.request.urlopen(req) as response:
        status = response.status
        if status not in (200, 204):
            print(f"❌ CRITICAL: Failed to store token to Vault (HTTP {status})", file=sys.stderr)
            print(response.read().decode('utf-8'), file=sys.stderr)
            sys.exit(1)
except urllib.error.HTTPError as e:
    print(f"❌ CRITICAL: Failed to store token to Vault (HTTP {e.code})", file=sys.stderr)
    print(e.read().decode('utf-8'), file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f"❌ CRITICAL: Failed to store token to Vault: {e}", file=sys.stderr)
    sys.exit(1)
PYTHON
  
  if [ $? -ne 0 ]; then
    echo "Token was generated but not persisted:" >&2
    echo "  ${TOKEN}" >&2
    exit 1
  fi
  echo "Root token stored to Vault successfully"
fi

echo "Root token initialization complete"
