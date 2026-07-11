#!/bin/bash
# Provision the truealpha raw-archive bucket + app-scoped S3 credentials.
#
# RUN ON THE VPS HOST (needs the docker CLI — the iac-runner deploys through
# the Dokploy API and has no docker socket, so deploy.py's _ensure_minio_bucket
# cannot do this itself; it points here instead).
#
# Semantics match platform/03.minio create_app_bucket with lifecycle_days=0:
# private bucket, NO lifecycle rules (the raw archive is the append-only
# point-in-time source of record — it must never auto-expire), bucket-scoped
# user, credentials stored in Vault. SSE-S3 is skipped: platform MinIO has no
# KMS configured (same tolerated state as finance_report's bucket).
#
# Usage:
#   VAULT_TOKEN=... bash provision_bucket.sh staging|production
#   (or pipe the token:  echo "$TOKEN" | bash provision_bucket.sh staging -)
#
# Idempotent: re-running refreshes the app user's secret and re-patches Vault;
# the bucket and policy are create-if-missing. After first provisioning,
# restart the app vault-agent so the new S3_* keys render:
#   docker restart truealpha-app-vault-agent[-staging] && sleep 5 \
#     && docker restart truealpha-web[-staging] truealpha-llm[-staging]

set -euo pipefail

ENV_NAME="${1:?usage: provision_bucket.sh staging|production}"
case "$ENV_NAME" in
  production) SUFFIX="" ;;
  staging)    SUFFIX="-staging" ;;
  *) echo "unsupported env: $ENV_NAME (staging|production)" >&2; exit 1 ;;
esac
if [ "${2:-}" = "-" ]; then
  read -r VAULT_TOKEN
fi
: "${VAULT_TOKEN:?set VAULT_TOKEN or pass '-' and pipe it on stdin}"

MINIO="platform-minio${SUFFIX}"
BUCKET="truealpha-raw"
APP_USER="truealpha_raw"
POLICY="truealpha_raw_readwrite"
VAULT_PATH="secret/truealpha/${ENV_NAME}/app"
M() { docker exec "$MINIO" "$@"; }

echo "== mc alias (from the container's own vault-rendered root creds)"
# The alias in a recreated container starts empty — never assume it works.
docker exec "$MINIO" sh -c '. /secrets/.env && mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null' && echo alias-ok

echo "== bucket (private, never-expire)"
M mc mb "local/$BUCKET" --ignore-existing
M mc anonymous set none "local/$BUCKET"
M mc ilm rm --all --force "local/$BUCKET" 2>&1 | tail -1 || true

echo "== app user (secret rotates on every run)"
SECRET=$(head -c 24 /dev/urandom | base64 | tr -d "/+=" | head -c 32)
if ! M mc admin user add local "$APP_USER" "$SECRET" >/dev/null 2>&1; then
  M mc admin user remove local "$APP_USER" >/dev/null 2>&1 || true
  M mc admin user add local "$APP_USER" "$SECRET" >/dev/null
fi
echo user-ok

echo "== bucket-scoped policy"
TMP_POLICY=$(mktemp)
cat > "$TMP_POLICY" <<POL
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["s3:GetBucketLocation","s3:ListBucket"],"Resource":["arn:aws:s3:::${BUCKET}"]},
 {"Effect":"Allow","Action":["s3:DeleteObject","s3:GetObject","s3:PutObject"],"Resource":["arn:aws:s3:::${BUCKET}/*"]}]}
POL
docker cp "$TMP_POLICY" "$MINIO:/tmp/${POLICY}.json"
rm -f "$TMP_POLICY"
M mc admin policy create local "$POLICY" "/tmp/${POLICY}.json" 2>&1 | tail -1 || true
M mc admin policy attach local "$POLICY" --user "$APP_USER" 2>&1 | tail -1 || true

echo "== vault ($VAULT_PATH)"
docker exec -e VAULT_TOKEN="$VAULT_TOKEN" -e VAULT_ADDR=http://127.0.0.1:8200 vault \
  vault kv patch "$VAULT_PATH" \
  S3_ACCESS_KEY="$APP_USER" S3_SECRET_KEY="$SECRET" S3_BUCKET="$BUCKET" >/dev/null
echo vault-patched

echo "== state"
M mc ls local/
M mc admin user info local "$APP_USER" | head -4
echo "done. Remember to restart the app vault-agent (+ web/llm) to render the new keys."
