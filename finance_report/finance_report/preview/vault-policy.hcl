# Vault policy for the PREVIEW app AppRole.
#
# Preview reads its app secrets from a FIXED source env (default: staging) — see
# secrets.ctmpl / PREVIEW_SECRET_ENV — because an ephemeral alias (pr-5, commit-...) has
# no Vault path of its own. So the preview AppRole needs READ on the SOURCE env's app
# path, NOT on a per-alias path. It does NOT need postgres/redis paths: a preview stack
# uses its own ephemeral DB and no shared Redis (DATABASE_URL is overridden locally).
#
# NOTE: the {{env}} placeholder here must be bound at provisioning time to the SOURCE
# secret env (staging by default), e.g. via `invoke vault.setup-approle` for the preview
# AppRole. This is a one-time LIVE setup; see the PR caveats.
path "secret/data/finance_report/{{env}}/app" {
  capabilities = ["read"]
}

path "secret/metadata/finance_report/{{env}}/app" {
  capabilities = ["read", "list"]
}

# Required for vault-agent AppRole auto_auth to validate/renew its token.
path "auth/token/lookup-self" {
  capabilities = ["read"]
}

path "auth/token/renew-self" {
  capabilities = ["update"]
}
