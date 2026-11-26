#!/usr/bin/env bash
set -euo pipefail

: "${INFRA2_DEPLOY_SHA:?INFRA2_DEPLOY_SHA is required}"

target_env="${IAC_RUNNER_TARGET_ENV:-production}"
runner_input_sha="${RUNNER_INPUT_SHA256:-}"
control_container="iac-runner"

case "$target_env" in
  production)
    target_container="iac-runner"
    dokploy_name="iac_runner"
    env_suffix=""
    allowed_envs="production"
    data_path="/data/bootstrap/iac-runner"
    subdomain="iac"
    image_repo="bootstrap-iac_runner-bkewyn-iac-runner"
    ;;
  staging)
    target_container="iac-runner-staging"
    dokploy_name="iac_runner_staging"
    env_suffix="-staging"
    allowed_envs="staging"
    data_path="/data/bootstrap/iac-runner-staging"
    subdomain="iac-staging"
    image_repo="bootstrap-iac_runner-staging-iac-runner"
    ;;
  *)
    echo "Unsupported IAC_RUNNER_TARGET_ENV='$target_env' (expected production or staging)" >&2
    exit 1
    ;;
esac

project="$(
  docker inspect "$target_container" \
    --format '{{ index .Config.Labels "com.docker.compose.project" }}' 2>/dev/null || true
)"

control_project="$(
  docker inspect iac-runner \
    --format '{{ index .Config.Labels "com.docker.compose.project" }}' 2>/dev/null || true
)"
control_code_dir=""
if [ -n "$control_project" ] && [ "$control_project" != "<no value>" ]; then
  control_code_dir="/etc/dokploy/compose/$control_project/code"
fi

if [ -z "$project" ] || [ "$project" = "<no value>" ]; then
  if [ "$target_env" != "staging" ]; then
    echo "Could not resolve Docker Compose project for $target_container" >&2
    exit 1
  fi
  echo "Cold-start bootstrap: resolving or creating Dokploy compose $dokploy_name for staging"
  project=""
fi

current_image="$(
  docker inspect "$target_container" \
    --format '{{.Config.Image}}' 2>/dev/null || echo "<none>"
)"
current_git_sha="$(
  docker inspect "$target_container" \
    --format '{{ index .Config.Labels "git.sha" }}' 2>/dev/null || echo ""
)"
current_runner_input_sha="$(
  docker inspect "$target_container" \
    --format '{{ index .Config.Labels "runner.input.sha256" }}' 2>/dev/null || echo ""
)"

env_file="$(mktemp)"
next_env_file="$(mktemp)"
cleanup() {
  rm -f "$env_file" "$next_env_file"
}
trap cleanup EXIT

echo "Persisting IaC Runner ($target_env) Dokploy settings"
dokploy_update_output="$(
  docker exec \
  -e "INFRA2_DEPLOY_SHA=$INFRA2_DEPLOY_SHA" \
  -e "INFRA2_DOKPLOY_APP_NAME=$project" \
  -e "INFRA2_DOKPLOY_NAME=$dokploy_name" \
  -e "INFRA2_TARGET_ENV=$target_env" \
  -e "INFRA2_RUNNER_INPUT_SHA256=$runner_input_sha" \
  -e "INFRA2_ENV_SUFFIX=$env_suffix" \
  -e "INFRA2_ALLOWED_ENVS=$allowed_envs" \
  -e "INFRA2_DATA_PATH=$data_path" \
  -e "INFRA2_SUBDOMAIN=$subdomain" \
  -e "INFRA2_IMAGE_REPO=$image_repo" \
  -i "$control_container" \
  sh -lc 'if [ -f /secrets/.env ]; then set -a; . /secrets/.env; set +a; fi; python -' <<'PY'
import base64
import json
import os
import subprocess
import urllib.parse
import urllib.request

deploy_sha = os.environ["INFRA2_DEPLOY_SHA"]
target_app_name = os.environ.get("INFRA2_DOKPLOY_APP_NAME", "")
target_name = os.environ["INFRA2_DOKPLOY_NAME"]
target_env = os.environ["INFRA2_TARGET_ENV"]
runner_input_sha = os.environ.get("INFRA2_RUNNER_INPUT_SHA256", "")
env_suffix = os.environ.get("INFRA2_ENV_SUFFIX", "")
allowed_envs = os.environ.get("INFRA2_ALLOWED_ENVS", "")
data_path = os.environ.get("INFRA2_DATA_PATH", "/data/bootstrap/iac-runner")
subdomain = os.environ.get("INFRA2_SUBDOMAIN", "iac")
image_repo = os.environ.get("INFRA2_IMAGE_REPO", "")
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
        "http://dokploy:3000/api",
        f"https://cloud.{internal_domain}/api" if internal_domain else "",
        "http://host.docker.internal:3000/api",
        "http://localhost:3000/api",
    ]
)


def request(base_url, method, endpoint, body=None):
    url = f"{base_url}/{endpoint.lstrip('/')}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "x-api-key": api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else None


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
    prod_compose = None
    bootstrap_env_id = None
    for project in projects:
        if project.get("name") != "bootstrap":
            continue
        for environment in project.get("environments", []):
            if bootstrap_env_id is None:
                bootstrap_env_id = environment.get("environmentId")
            for compose in environment.get("compose", []):
                if target_app_name and compose.get("appName") == target_app_name:
                    return compose, prod_compose, environment.get("environmentId")
                if compose.get("name") == "iac_runner":
                    prod_compose = compose
                if compose.get("name") == target_name:
                    matches.append((compose, environment.get("environmentId")))
    if len(matches) == 1:
        return matches[0][0], prod_compose, matches[0][1]
    if target_env == "staging" and prod_compose and bootstrap_env_id:
        return None, prod_compose, bootstrap_env_id
    raise SystemExit(
        f"Could not uniquely resolve bootstrap/{target_name} compose in Dokploy"
    )


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


def provision_staging_vault(prod_details):
    root_token = subprocess.check_output(
        ["op", "read", "op://Infra2/bootstrap/vault/Root Token/Root Token"],
        text=True,
        timeout=30,
    ).strip()
    vault_addr = get_env_value(prod_details.get("env") or "", "VAULT_ADDR") or (
        f"https://vault.{internal_domain}" if internal_domain else "http://vault:8200"
    )
    vault_env = {**os.environ, "VAULT_ADDR": vault_addr, "VAULT_TOKEN": root_token}
    # Copy bootstrap iac_runner secret to staging if missing.
    existing = subprocess.run(
        ["vault", "kv", "get", "-format=json", "secret/bootstrap/staging/iac_runner"],
        env=vault_env,
        capture_output=True,
        text=True,
        check=False,
    )
    if existing.returncode != 0:
        prod_kv = json.loads(
            subprocess.check_output(
                ["vault", "kv", "get", "-format=json", "secret/bootstrap/production/iac_runner"],
                env=vault_env,
                text=True,
            )
        )["data"]["data"]
        args = ["vault", "kv", "put", "secret/bootstrap/staging/iac_runner"] + [
            f"{k}={v}" for k, v in prod_kv.items()
        ]
        subprocess.run(args, env=vault_env, check=True)
    policy = """path "auth/token/renew-self" { capabilities = ["update"] }
path "auth/token/lookup-self" { capabilities = ["read"] }
path "secret/data/bootstrap/staging/iac_runner" { capabilities = ["read", "list"] }
path "secret/data/platform/staging/*" { capabilities = ["create", "read", "update", "patch", "list"] }
path "secret/data/finance_report/staging/*" { capabilities = ["create", "read", "update", "patch", "list"] }
path "secret/data/truealpha/staging/*" { capabilities = ["create", "read", "update", "patch", "list"] }
path "secret/metadata/platform/staging/*" { capabilities = ["read", "list"] }
path "secret/metadata/finance_report/staging/*" { capabilities = ["read", "list"] }
path "secret/metadata/truealpha/staging/*" { capabilities = ["read", "list"] }
"""
    role = "bootstrap-staging-iac_runner"
    subprocess.run(
        ["vault", "policy", "write", role, "-"],
        input=policy,
        text=True,
        env=vault_env,
        check=True,
    )
    subprocess.run(
        [
            "vault",
            "write",
            f"auth/approle/role/{role}",
            f"token_policies={role}",
            "token_no_default_policy=true",
            "token_ttl=24h",
            "token_max_ttl=168h",
            "secret_id_num_uses=0",
            "secret_id_ttl=0",
        ],
        env=vault_env,
        check=True,
    )
    role_id = json.loads(
        subprocess.check_output(
            ["vault", "read", "-format=json", f"auth/approle/role/{role}/role-id"],
            env=vault_env,
            text=True,
        )
    )["data"]["role_id"]
    secret_id = json.loads(
        subprocess.check_output(
            ["vault", "write", "-f", "-format=json", f"auth/approle/role/{role}/secret-id"],
            env=vault_env,
            text=True,
        )
    )["data"]["secret_id"]
    return vault_addr, role_id, secret_id


api_base, projects = select_api_base()
compose, prod_compose, env_id = find_iac_runner_compose(projects)
if compose is None:
    created = request(
        api_base,
        "POST",
        "compose.create",
        {
            "environmentId": env_id,
            "name": target_name,
            "appName": f"bootstrap-{target_name}",
            "composeType": "docker-compose",
        },
    )
    compose_id = created["composeId"]
    prod_details = request(
        api_base,
        "GET",
        "compose.one?" + urllib.parse.urlencode({"composeId": prod_compose["composeId"]}),
    )
    request(
        api_base,
        "POST",
        "compose.update",
        {
            "composeId": compose_id,
            "sourceType": prod_details.get("sourceType") or "github",
            "githubId": prod_details.get("githubId"),
            "repository": prod_details.get("repository") or "infra2",
            "owner": prod_details.get("owner") or "wangzitian0",
            "branch": prod_details.get("branch") or "main",
            "composePath": prod_details.get("composePath") or "bootstrap/06.iac_runner/compose.yaml",
            "autoDeploy": False,
        },
    )
    if internal_domain:
        request(
            api_base,
            "POST",
            "domain.create",
            {
                "composeId": compose_id,
                "host": f"{subdomain}.{internal_domain}",
                "port": 8080,
                "https": True,
                "certificateType": "letsencrypt",
                "serviceName": "iac-runner",
                "domainType": "compose",
            },
        )
    _, projects = select_api_base()
    compose, prod_compose, _ = find_iac_runner_compose(projects)
else:
    prod_details = (
        request(
            api_base,
            "GET",
            "compose.one?" + urllib.parse.urlencode({"composeId": prod_compose["composeId"]}),
        )
        if prod_compose
        else {}
    )

compose_id = compose["composeId"]
resolved_app_name = compose.get("appName") or target_app_name
print(f"Selected Dokploy API base: {api_base}")
print(
    "Dokploy compose before update: "
    f"composeId={compose_id} "
    f"appName={resolved_app_name} "
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
if target_env == "staging" and (
    not get_env_value(current_env, "VAULT_ROLE_ID")
    or not get_env_value(current_env, "VAULT_SECRET_ID")
):
    vault_addr, role_id, secret_id = provision_staging_vault(prod_details or details)
    current_env = set_env_value(current_env, "VAULT_ADDR", vault_addr)
    current_env = set_env_value(current_env, "VAULT_ROLE_ID", role_id)
    current_env = set_env_value(current_env, "VAULT_SECRET_ID", secret_id)
    if not get_env_value(current_env, "INTERNAL_DOMAIN") and internal_domain:
        current_env = set_env_value(current_env, "INTERNAL_DOMAIN", internal_domain)

current_git_sha = get_env_value(current_env, "GIT_SHA")
print(
    "Dokploy compose env before update: "
    f"line_count={len(current_env.splitlines())} "
    f"git_sha={'<unset>' if current_git_sha is None else current_git_sha}"
)
next_env = set_env_value(current_env, "GIT_SHA", deploy_sha[:7])
next_env = set_env_value(next_env, "ENV", target_env)
next_env = set_env_value(next_env, "ENV_SUFFIX", env_suffix)
next_env = set_env_value(next_env, "DATA_PATH", data_path)
next_env = set_env_value(next_env, "IAC_RUNNER_ALLOWED_ENVS", allowed_envs)
if image_repo:
    next_env = set_env_value(next_env, "IAC_RUNNER_IMAGE_REPO", image_repo)
if runner_input_sha:
    next_env = set_env_value(next_env, "RUNNER_INPUT_SHA256", runner_input_sha)

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
print(f"INFRA2_TARGET_PROJECT={resolved_app_name}")
print(
    "INFRA2_CONFIRMED_ENV_B64="
    + base64.b64encode(confirmed_env.encode()).decode()
)
PY
)"

printf '%s\n' "$dokploy_update_output" \
  | sed '/^INFRA2_CONFIRMED_ENV_B64=/d;/^INFRA2_TARGET_PROJECT=/d'

resolved_project="$(
  printf '%s\n' "$dokploy_update_output" \
    | sed -n 's/^INFRA2_TARGET_PROJECT=//p' \
    | tail -n1
)"
if [ -z "$project" ]; then
  project="$resolved_project"
fi
if [ -z "$project" ] || [ "$project" = "<no value>" ]; then
  echo "Could not resolve Docker Compose project for $target_container" >&2
  exit 1
fi

compose_root="/etc/dokploy/compose/$project"
code_dir="$compose_root/code"
compose_file="$code_dir/bootstrap/06.iac_runner/compose.yaml"
traefik_override="$compose_root/traefik.override.yml"

mkdir -p "$compose_root" "$data_path/workspace"
if [ ! -e "$code_dir/.git" ] && [ -n "$control_code_dir" ] && [ -d "$control_code_dir/.git" ]; then
  cp -a "$control_code_dir" "$code_dir"
fi
if [ ! -e "$traefik_override" ]; then
  cat > "$traefik_override" <<YAML
services:
  iac-runner:
    labels:
      - traefik.enable=true
      - traefik.docker.network=dokploy-network
      - traefik.http.routers.${project}-websecure.rule=Host(\`${subdomain}.${INTERNAL_DOMAIN:-zitian.party}\`)
      - traefik.http.routers.${project}-websecure.entrypoints=websecure
      - traefik.http.routers.${project}-websecure.tls.certresolver=letsencrypt
      - traefik.http.services.${project}.loadbalancer.server.port=8080
YAML
fi

echo "IaC Runner bootstrap target:"
echo "  target_env=$target_env"
echo "  deploy_sha=$INFRA2_DEPLOY_SHA"
echo "  compose_project=$project"
echo "  target_container=$target_container"
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
git -C "$code_dir" checkout -f "$INFRA2_DEPLOY_SHA" -- bootstrap/06.iac_runner uv.lock tools/sdk_requirement.py
echo "IaC Runner bootstrap source HEAD: $(git -C "$code_dir" rev-parse --short HEAD)"

# shellcheck source=SCRIPTDIR/../bootstrap/06.iac_runner/wait_for_idle.sh
. "$code_dir/bootstrap/06.iac_runner/wait_for_idle.sh"
export IAC_RUNNER_CONTAINER="$target_container"

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

if ! grep -q '^VAULT_ROLE_ID=' "$env_file" || ! grep -q '^VAULT_SECRET_ID=' "$env_file"; then
  echo "Dokploy compose env is missing VAULT_ROLE_ID/VAULT_SECRET_ID; refusing to recreate $target_container" >&2
  echo "Run: invoke vault.setup-approle --project=bootstrap --service=iac_runner" >&2
  exit 1
fi
if ! grep -q '^DOKPLOY_API_KEY=' "$env_file"; then
  current_dokploy_api_key="$(
    docker inspect "$control_container" --format '{{range .Config.Env}}{{println .}}{{end}}' \
      | sed -n 's/^DOKPLOY_API_KEY=//p' \
      | tail -n1
  )"
  if [ -n "$current_dokploy_api_key" ]; then
    printf 'DOKPLOY_API_KEY=%s\n' "$current_dokploy_api_key" >> "$env_file"
    echo "Recovered DOKPLOY_API_KEY from current container env for bootstrap rebuild ($control_container)"
  else
    echo "Dokploy compose env and current container env are missing DOKPLOY_API_KEY; continuing because Vault-rendered secrets may provide it at runtime"
  fi
fi

# BEGIN runner_inputs_fingerprint
# The object ids of every input that the image build reads: the same paths that make
# reconcile-iac-inputs.yml run this script.
runner_inputs_fingerprint() {
  local repo="$1" sha="$2"
  {
    git -C "$repo" ls-tree "$sha" -- bootstrap/06.iac_runner scripts/deploy_iac_runner_bootstrap.sh uv.lock
    git -C "$repo" ls-tree "$sha" -- tools/ | grep -E '[[:space:]]tools/sdk_requirement[^/]*\.py$' || true
  } 2>/dev/null
}
# END runner_inputs_fingerprint

current_health="$(
  docker inspect "$target_container" \
    --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null || echo none
)"
if [ "$current_health" = "healthy" ]; then
  if [ -n "$runner_input_sha" ] && [ "$current_runner_input_sha" = "$runner_input_sha" ]; then
    echo "IaC Runner ($target_env) matches runner_input_sha256=$runner_input_sha and container is healthy; skipping rebuild"
    exit 0
  fi
  if [ -z "$runner_input_sha" ] && [ -n "$current_git_sha" ] && [ "$current_git_sha" != "<no value>" ]; then
    if git -C "$code_dir" rev-parse -q --verify "${current_git_sha}^{commit}" >/dev/null 2>&1; then
      current_runner_inputs="$(runner_inputs_fingerprint "$code_dir" "$current_git_sha")"
      target_runner_inputs="$(runner_inputs_fingerprint "$code_dir" "$INFRA2_DEPLOY_SHA")"
      if [ -n "$current_runner_inputs" ] && [ "$current_runner_inputs" = "$target_runner_inputs" ]; then
        echo "IaC Runner inputs unchanged from $current_git_sha and container is healthy; skipping image rebuild and recreate"
        exit 0
      fi
    fi
  fi
fi

echo "Building IaC Runner image for compose project $project"
docker compose \
  -p "$project" \
  --env-file "$env_file" \
  -f "$compose_file" \
  -f "$traefik_override" \
  build </dev/null

# The recreate drops the runner's in-memory in-flight deploy state (#666). Wait after
# the build, so a deploy that starts during a slow build also delays the recreate.
wait_for_runner_idle

echo "Rebuilding and recreating IaC Runner compose project $project"
docker compose \
  -p "$project" \
  --env-file "$env_file" \
  -f "$compose_file" \
  -f "$traefik_override" \
  up -d --build --force-recreate

for _attempt in $(seq 1 30); do
  health="$(
    docker inspect "$target_container" \
      --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}'
  )"
  running_image="$(
    docker inspect "$target_container" \
      --format '{{.Config.Image}}'
  )"
  running_git_sha="$(
    docker inspect "$target_container" \
      --format '{{ index .Config.Labels "git.sha" }}'
  )"
  echo "IaC Runner health attempt ${_attempt}/30: health=$health image=$running_image git_sha=${running_git_sha:-<none>}"
  if [ "$health" = "healthy" ]; then
    echo "IaC Runner is healthy after bootstrap update"
    exit 0
  fi
  sleep 5
done

echo "Health check timed out for iac-runner ($target_container)" >&2
docker ps --filter "name=$target_container" --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
docker logs --tail=80 "$target_container" >&2
exit 1
