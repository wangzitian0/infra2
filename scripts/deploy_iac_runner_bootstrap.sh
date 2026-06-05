#!/usr/bin/env bash
set -euo pipefail

: "${INFRA2_DEPLOY_SHA:?INFRA2_DEPLOY_SHA is required}"

project="$(
  docker inspect iac-runner \
    --format '{{ index .Config.Labels "com.docker.compose.project" }}'
)"

if [ -z "$project" ] || [ "$project" = "<no value>" ]; then
  echo "Could not resolve Docker Compose project for iac-runner" >&2
  exit 1
fi

compose_root="/etc/dokploy/compose/$project"
code_dir="$compose_root/code"
compose_file="$code_dir/bootstrap/06.iac_runner/compose.yaml"
traefik_override="$compose_root/traefik.override.yml"

for required_path in "$code_dir/.git" "$compose_file" "$traefik_override"; do
  if [ ! -e "$required_path" ]; then
    echo "Required IaC Runner bootstrap path is missing: $required_path" >&2
    exit 1
  fi
done

echo "Updating IaC Runner bootstrap source to $INFRA2_DEPLOY_SHA"
git -C "$code_dir" fetch --tags --prune origin
git -C "$code_dir" checkout -f "$INFRA2_DEPLOY_SHA" -- bootstrap/06.iac_runner

env_file="$(mktemp)"
next_env_file="$(mktemp)"
cleanup() {
  rm -f "$env_file" "$next_env_file"
}
trap cleanup EXIT

docker inspect iac-runner --format '{{range .Config.Env}}{{println .}}{{end}}' \
  > "$env_file"
grep -v '^GIT_SHA=' "$env_file" > "$next_env_file" || true
printf 'GIT_SHA=%.7s\n' "$INFRA2_DEPLOY_SHA" >> "$next_env_file"
mv "$next_env_file" "$env_file"

echo "Rebuilding and recreating IaC Runner compose project $project"
docker compose \
  -p "$project" \
  --env-file "$env_file" \
  -f "$compose_file" \
  -f "$traefik_override" \
  up -d --build --force-recreate

for _attempt in $(seq 1 30); do
  health="$(
    docker inspect iac-runner \
      --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}'
  )"
  if [ "$health" = "healthy" ]; then
    echo "IaC Runner is healthy after bootstrap update"
    exit 0
  fi
  sleep 5
done

echo "Health check timed out for iac-runner" >&2
docker ps --filter name=iac-runner --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
docker logs --tail=80 iac-runner >&2
exit 1
