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

current_image="$(
  docker inspect iac-runner \
    --format '{{.Config.Image}}'
)"
current_git_sha="$(
  docker inspect iac-runner \
    --format '{{ index .Config.Labels "git.sha" }}'
)"

compose_root="/etc/dokploy/compose/$project"
code_dir="$compose_root/code"
compose_file="$code_dir/bootstrap/06.iac_runner/compose.yaml"
traefik_override="$compose_root/traefik.override.yml"

echo "IaC Runner bootstrap target:"
echo "  deploy_sha=$INFRA2_DEPLOY_SHA"
echo "  compose_project=$project"
echo "  current_image=$current_image"
echo "  current_git_sha=${current_git_sha:-<none>}"
echo "  code_dir=$code_dir"
echo "  compose_file=$compose_file"

for required_path in "$code_dir/.git" "$compose_file" "$traefik_override"; do
  if [ ! -e "$required_path" ]; then
    echo "Required IaC Runner bootstrap path is missing: $required_path" >&2
    exit 1
  fi
done

echo "Updating IaC Runner bootstrap source to $INFRA2_DEPLOY_SHA"
git -C "$code_dir" fetch --tags --prune origin '+refs/heads/*:refs/remotes/origin/*'
git -C "$code_dir" checkout -f "$INFRA2_DEPLOY_SHA" -- bootstrap/06.iac_runner
echo "IaC Runner bootstrap source HEAD: $(git -C "$code_dir" rev-parse --short HEAD)"

env_file="$(mktemp)"
next_env_file="$(mktemp)"
cleanup() {
  rm -f "$env_file" "$next_env_file"
}
trap cleanup EXIT

echo "Persisting IaC Runner Dokploy settings"
dokploy_update_output="$(
  docker exec \
  -e "INFRA2_DEPLOY_SHA=$INFRA2_DEPLOY_SHA" \
  -e "INFRA2_DOKPLOY_APP_NAME=$project" \
  -i iac-runner \
  sh -lc 'if [ -f /secrets/.env ]; then set -a; . /secrets/.env; set +a; fi; python -' <<'PY'
import base64
import json
import os
import urllib.parse
import urllib.request

deploy_sha = os.environ["INFRA2_DEPLOY_SHA"]
target_app_name = os.environ["INFRA2_DOKPLOY_APP_NAME"]
api_key = os.environ.get("DOKPLOY_API_KEY", "")
internal_domain = os.environ.get("INTERNAL_DOMAIN", "")

if not api_key:
    raise SystemExit("DOKPLOY_API_KEY is required in IaC Runner env or rendered secrets")


def unique(items):
    seen = set()
    result = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item.rstrip("/"))
    return result


base_urls = unique(
    [
        os.environ.get("DOKPLOY_INTERNAL_URL"),
        "http://dokploy:3000/api",
        os.environ.get("DOKPLOY_URL"),
        f"https://cloud.{internal_domain}/api" if internal_domain else None,
    ]
)


def request(base_url, method, endpoint, payload=None):
    data = None
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "x-api-key": api_key,
        "user-agent": "infra2-iac-runner-bootstrap/1.0",
    }
    if payload is not None:
        data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{base_url}/{endpoint}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
    return json.loads(body.decode()) if body else {}


def select_api_base():
    errors = []
    for base_url in base_urls:
        try:
            return base_url, request(base_url, "GET", "project.all")
        except Exception as exc:
            errors.append(f"{base_url}: {type(exc).__name__}")
    raise SystemExit(
        "Could not reach Dokploy API through any configured base URL: "
        + "; ".join(errors)
    )


def find_iac_runner_compose(projects):
    matches = []
    for project in projects:
        if project.get("name") != "bootstrap":
            continue
        for environment in project.get("environments", []):
            for compose in environment.get("compose", []):
                if compose.get("appName") == target_app_name:
                    return compose
                if compose.get("name") == "iac_runner":
                    matches.append(compose)
    if len(matches) == 1:
        return matches[0]
    raise SystemExit("Could not uniquely resolve bootstrap/iac_runner compose in Dokploy")


def set_env_value(env_text, key, value):
    next_lines = []
    replaced = False
    for line in env_text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and stripped.split("=", 1)[0] == key:
            next_lines.append(f"{key}={value}")
            replaced = True
        else:
            next_lines.append(line)
    if not replaced:
        next_lines.append(f"{key}={value}")
    return "\n".join(next_lines).rstrip() + "\n"


def get_env_value(env_text, key):
    for line in env_text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and stripped.split("=", 1)[0] == key:
            return stripped.split("=", 1)[1]
    return None


api_base, projects = select_api_base()
compose = find_iac_runner_compose(projects)
compose_id = compose["composeId"]
print(f"Selected Dokploy API base: {api_base}")
print(
    "Dokploy compose before update: "
    f"composeId={compose_id} "
    f"appName={compose.get('appName')} "
    f"name={compose.get('name')} "
    f"autoDeploy={compose.get('autoDeploy')} "
    f"triggerType={compose.get('triggerType')}"
)
details = request(
    api_base,
    "GET",
    "compose.one?" + urllib.parse.urlencode({"composeId": compose_id}),
)
current_env = details.get("env") or ""
current_git_sha = get_env_value(current_env, "GIT_SHA")
print(
    "Dokploy compose env before update: "
    f"line_count={len(current_env.splitlines())} "
    f"git_sha={'<unset>' if current_git_sha is None else current_git_sha}"
)
next_env = set_env_value(current_env, "GIT_SHA", deploy_sha[:7])

request(
    api_base,
    "POST",
    "compose.update",
    {
        "composeId": compose_id,
        "env": next_env,
        "autoDeploy": False,
    },
)
confirmed = request(
    api_base,
    "GET",
    "compose.one?" + urllib.parse.urlencode({"composeId": compose_id}),
)
confirmed_env = confirmed.get("env") or ""
print(
    "Dokploy compose after update: "
    f"composeId={compose_id} "
    f"autoDeploy={confirmed.get('autoDeploy')} "
    f"git_sha={get_env_value(confirmed_env, 'GIT_SHA')}"
)
print(
    "INFRA2_CONFIRMED_ENV_B64="
    + base64.b64encode(confirmed_env.encode()).decode()
)
PY
)"

printf '%s\n' "$dokploy_update_output" \
  | sed '/^INFRA2_CONFIRMED_ENV_B64=/d'

confirmed_env_b64="$(
  printf '%s\n' "$dokploy_update_output" \
    | sed -n 's/^INFRA2_CONFIRMED_ENV_B64=//p' \
    | tail -n1
)"
if [ -z "$confirmed_env_b64" ]; then
  echo "Dokploy compose update did not return confirmed env" >&2
  exit 1
fi
printf '%s' "$confirmed_env_b64" | base64 -d > "$env_file"

if ! grep -q '^VAULT_APP_TOKEN=' "$env_file"; then
  echo "Dokploy compose env is missing VAULT_APP_TOKEN; refusing to recreate IaC Runner" >&2
  exit 1
fi
if ! grep -q '^DOKPLOY_API_KEY=' "$env_file"; then
  current_dokploy_api_key="$(
    docker inspect iac-runner --format '{{range .Config.Env}}{{println .}}{{end}}' \
      | sed -n 's/^DOKPLOY_API_KEY=//p' \
      | tail -n1
  )"
  if [ -n "$current_dokploy_api_key" ]; then
    printf 'DOKPLOY_API_KEY=%s\n' "$current_dokploy_api_key" >> "$env_file"
    echo "Recovered DOKPLOY_API_KEY from current container env for bootstrap rebuild"
  else
    echo "Dokploy compose env and current container env are missing DOKPLOY_API_KEY; continuing because Vault-rendered secrets may provide it at runtime"
  fi
fi

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
  running_image="$(
    docker inspect iac-runner \
      --format '{{.Config.Image}}'
  )"
  running_git_sha="$(
    docker inspect iac-runner \
      --format '{{ index .Config.Labels "git.sha" }}'
  )"
  echo "IaC Runner health attempt ${_attempt}/30: health=$health image=$running_image git_sha=${running_git_sha:-<none>}"
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
