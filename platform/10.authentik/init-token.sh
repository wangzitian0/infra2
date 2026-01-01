#!/bin/sh
# Authentik Root Token initialization script
# Creates admin API token and stores as 'root_token' in Vault

set -e
set -o pipefail

# Wait for Authentik to be ready
echo "Waiting for Authentik to be ready..."
until ak healthcheck > /dev/null 2>&1; do
  sleep 5
done

echo "Authentik is ready, checking for root token..."

# Create API token using Django shell and capture output
TOKEN=$(python -m manage shell << 'PYTHON'
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
  export VAULT_TOKEN="$VAULT_INIT_TOKEN"
  
  # Use VAULT_INIT_ADDR directly (passed from compose.yaml)
  VAULT_URL="${VAULT_INIT_ADDR:-https://vault.zitian.party}"
  ENV_NAME="${ENV:-production}"
  if vault kv patch -address="$VAULT_URL" "secret/platform/${ENV_NAME}/authentik" root_token="$TOKEN"; then
    echo "Root token stored to Vault successfully"
  else
    echo "Warning: Failed to store token to Vault"
  fi
fi

echo "Root token initialization complete"
