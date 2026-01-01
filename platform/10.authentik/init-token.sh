#!/bin/sh
# Authentik Root Token initialization script
# Creates admin API token and stores as 'root_token' in Vault

set -e

# Wait for Authentik to be ready
echo "Waiting for Authentik to be ready..."
until ak healthcheck > /dev/null 2>&1; do
  sleep 5
done

echo "Authentik is ready, checking for root token..."

# Create API token using Django shell
python -m manage shell << 'PYTHON'
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
    print(f"Error: {e}")
    exit(1)
PYTHON

echo "Root token initialization complete"
