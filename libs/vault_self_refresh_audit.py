"""Read-only Vault self-refresh audit helpers.

The module is intentionally split into pure classifiers and thin live collection
adapters. Tests exercise the classifiers and inventory/static contracts without
requiring live Vault, Dokploy, or SSH access.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import json
import re
import shlex
import subprocess
import time
from typing import Any

from libs.env import verify_vault_token


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVENTORY_PATH = REPO_ROOT / "docs/ssot/vault-self-refresh-inventory.yaml"
SECRET_KEYS = ("token", "secret", "password", "key", "authorization")
ERROR_LOG_PATTERNS = (
    "permission denied",
    "token expired",
    "token is expired",
    "no handler for route",
    "template render failed",
    "template rendering failed",
    "failed rendering",
    "error rendering",
    "vault connection",
    "connection refused",
    "context deadline exceeded",
    "VAULT_APP_TOKEN is required",
    # AppRole-auth services (#257/#259) crash-loop with this instead of the
    # legacy VAULT_APP_TOKEN message when their role_id/secret_id are unset/wiped.
    "VAULT_ROLE_ID and VAULT_SECRET_ID are required",
)


@dataclass(frozen=True)
class VaultService:
    id: str
    project: str
    dokploy_service: str
    compose_path: str
    vault_agent_config_path: str
    secret_template_path: str
    vault_path_template: str
    vault_agent_container: str
    app_containers: tuple[str, ...]
    vault_token_env_key: str = "VAULT_APP_TOKEN"
    rendered_secret_path: str = "/vault/secrets/.env"
    app_secret_mount_path: str = "/secrets/.env"
    max_rendered_secret_age_seconds: int = 900
    min_token_ttl_hours: int = 48
    # "token" = static VAULT_APP_TOKEN; "approle" = VAULT_ROLE_ID + VAULT_SECRET_ID.
    auth_method: str = "token"

    def vault_path(self, env: str) -> str:
        return self.vault_path_template.format(env=env)

    @property
    def auth_env_keys(self) -> tuple[str, ...]:
        """Env keys the vault-agent must carry for this service's auth method."""
        if self.auth_method == "approle":
            return ("VAULT_ROLE_ID", "VAULT_SECRET_ID")
        return (self.vault_token_env_key,)


@dataclass
class CheckResult:
    service_id: str
    check_id: str
    status: str
    severity: str
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_inventory(path: Path | str = DEFAULT_INVENTORY_PATH) -> list[VaultService]:
    try:
        import yaml
    except ModuleNotFoundError as exc:
        if exc.name == "yaml":
            raise RuntimeError(
                "PyYAML is required to load the Vault self-refresh inventory."
            ) from exc
        raise

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    defaults = data.get("defaults", {})
    services: list[VaultService] = []
    for raw_service in data.get("services", []):
        merged = {**defaults, **raw_service}
        merged["app_containers"] = tuple(merged.get("app_containers", ()))
        services.append(VaultService(**merged))
    return services


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "***REDACTED***" if _is_secret_key(key) else redact(val)
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def parse_env(env_text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_line in env_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip().strip('"').strip("'")
    return parsed


def classify_token(
    service: VaultService,
    env_text: str,
    lookup: dict[str, Any] | None,
) -> CheckResult:
    env = parse_env(env_text)
    token = env.get(service.vault_token_env_key)
    if not token:
        return _result(
            service,
            "dokploy-env-token",
            "fail",
            "P0",
            f"{service.vault_token_env_key} is missing from Dokploy env",
            {"env_keys": sorted(env.keys())},
        )
    if not _looks_like_vault_token(token):
        return _result(
            service,
            "dokploy-env-token",
            "fail",
            "P0",
            f"{service.vault_token_env_key} is malformed",
            {"token": token},
        )
    if lookup is None:
        return _result(
            service,
            "vault-token-lookup",
            "fail",
            "P0",
            "Vault token lookup did not run",
            {},
        )
    if not lookup.get("valid"):
        return _result(
            service,
            "vault-token-lookup",
            "fail",
            "P0",
            "Vault token lookup failed",
            lookup,
        )
    if not lookup.get("renewable"):
        return _result(
            service,
            "vault-token-renewable",
            "fail",
            "P0",
            "Vault app token is not renewable",
            lookup,
        )
    ttl = float(lookup.get("ttl_hours", -1))
    if ttl < service.min_token_ttl_hours:
        return _result(
            service,
            "vault-token-ttl",
            "fail",
            "P1",
            f"Vault app token TTL is below {service.min_token_ttl_hours}h",
            lookup,
        )
    return _result(
        service,
        "vault-token",
        "pass",
        "P0",
        "Vault app token is valid, renewable, and above TTL floor",
        lookup,
    )


def classify_rendered_env(
    service: VaultService,
    file_state: dict[str, Any],
    now: int | None = None,
) -> CheckResult:
    if not file_state.get("exists"):
        return _result(
            service,
            "rendered-env",
            "fail",
            "P0",
            f"{service.rendered_secret_path} is missing",
            file_state,
        )
    if not file_state.get("readable", True):
        return _result(
            service,
            "rendered-env",
            "fail",
            "P0",
            f"{service.rendered_secret_path} is not readable",
            file_state,
        )
    if int(file_state.get("size", 0)) <= 0:
        return _result(
            service,
            "rendered-env",
            "fail",
            "P0",
            f"{service.rendered_secret_path} is empty",
            file_state,
        )
    if file_state.get("has_no_value"):
        return _result(
            service,
            "rendered-env-template-values",
            "fail",
            "P0",
            f"{service.rendered_secret_path} contains unresolved Vault template values",
            file_state,
        )
    observed_now = int(now if now is not None else time.time())
    mtime = int(file_state.get("mtime", 0))
    age = max(0, observed_now - mtime)
    evidence = {**file_state, "age_seconds": age}
    if age > service.max_rendered_secret_age_seconds:
        return _result(
            service,
            "rendered-env-freshness",
            "fail",
            "P1",
            f"{service.rendered_secret_path} is stale",
            evidence,
        )
    return _result(
        service,
        "rendered-env",
        "pass",
        "P0",
        f"{service.rendered_secret_path} is present and fresh",
        evidence,
    )


def classify_vault_agent_logs(service: VaultService, logs: str) -> CheckResult:
    lower_logs = logs.lower()
    matches = [
        pattern for pattern in ERROR_LOG_PATTERNS if pattern.lower() in lower_logs
    ]
    if matches:
        return _result(
            service,
            "vault-agent-logs",
            "fail",
            "P1",
            "vault-agent logs contain refresh/render errors",
            {"matched_patterns": matches, "log_excerpt": _safe_excerpt(logs)},
        )
    return _result(
        service,
        "vault-agent-logs",
        "pass",
        "P1",
        "vault-agent logs have no known refresh/render error patterns",
        {"checked_patterns": list(ERROR_LOG_PATTERNS)},
    )


def classify_container(
    service: VaultService,
    container_state: dict[str, Any],
    *,
    check_id: str,
    expected_mount: str | None = None,
) -> CheckResult:
    name = str(container_state.get("name") or "")
    if not container_state.get("exists", True):
        return _result(
            service,
            check_id,
            "fail",
            "P0",
            f"container {name or '<unknown>'} is missing",
            container_state,
        )
    if str(container_state.get("state", "")).lower() != "running":
        return _result(
            service,
            check_id,
            "fail",
            "P0",
            f"container {name} is not running",
            container_state,
        )
    health = str(container_state.get("health", "healthy")).lower()
    if health not in {"healthy", "none", ""}:
        return _result(
            service,
            check_id,
            "fail",
            "P0",
            f"container {name} health is {health}",
            container_state,
        )
    restart_count = int(container_state.get("restart_count", 0))
    if restart_count > int(container_state.get("max_restart_count", 3)):
        return _result(
            service,
            check_id,
            "fail",
            "P1",
            f"container {name} restart count is high",
            container_state,
        )
    if expected_mount:
        mounts = container_state.get("mounts", [])
        has_mount = expected_mount in mounts or any(
            expected_mount.startswith(f"{str(mount).rstrip('/')}/") for mount in mounts
        )
        if not has_mount:
            return _result(
                service,
                check_id,
                "fail",
                "P1",
                f"container {name} is missing mount {expected_mount}",
                container_state,
            )
    return _result(
        service,
        check_id,
        "pass",
        "P0",
        f"container {name} is running with acceptable health",
        container_state,
    )


def audit_from_observations(
    services: list[VaultService],
    observations: dict[str, Any],
    *,
    env: str,
    now: int | None = None,
) -> dict[str, Any]:
    results: list[CheckResult] = []
    observed_services = observations.get("services", {})
    for service in services:
        obs = observed_services.get(service.id, {})
        env_text = str(obs.get("dokploy_env", ""))
        lookup = obs.get("token_lookup")
        results.append(classify_token(service, env_text, lookup))
        results.append(classify_rendered_env(service, obs.get("rendered_env", {}), now))
        results.append(
            classify_vault_agent_logs(service, str(obs.get("vault_agent_logs", "")))
        )
        vault_agent_state = obs.get("vault_agent_container", {})
        results.append(
            classify_container(
                service,
                vault_agent_state,
                check_id="vault-agent-container",
            )
        )
        for app_state in obs.get("app_containers", []):
            results.append(
                classify_container(
                    service,
                    app_state,
                    check_id="app-container",
                    expected_mount=service.app_secret_mount_path,
                )
            )
    status = "pass" if all(item.status == "pass" for item in results) else "fail"
    return {
        "schema_version": 1,
        "env": env,
        "status": status,
        "generated_at": int(now if now is not None else time.time()),
        "results": [redact(result.to_dict()) for result in results],
    }


def collect_live_observations(
    services: list[VaultService],
    *,
    env: str,
    host: str | None = None,
) -> dict[str, Any]:
    """Collect read-only live observations from Dokploy, Vault, and Docker.

    This function intentionally only reads state. It does not restart, mutate,
    renew, or rotate anything.
    """
    from libs.common import get_env
    from libs.dokploy import get_dokploy

    env_vars = get_env()
    vps_host = host or env_vars.get("VPS_HOST")
    if not vps_host:
        raise ValueError("VPS_HOST is required for live audit")
    internal_domain = env_vars.get("INTERNAL_DOMAIN")
    vault_addr = _vault_addr_from_env(env_vars)
    dokploy_host = f"cloud.{internal_domain}" if internal_domain else None
    client = get_dokploy(host=dokploy_host)
    observations: dict[str, Any] = {"services": {}}
    for service in services:
        compose = client.find_compose_by_name(
            service.dokploy_service,
            project_name=service.project,
            env_name=env,
        )
        env_text = compose.get("env", "") if compose else ""
        token = parse_env(env_text).get(service.vault_token_env_key)
        token_lookup = (
            verify_vault_token(
                token,
                addr=vault_addr,
                min_ttl_hours=service.min_token_ttl_hours,
            )
            if token
            else None
        )
        vault_agent_name = _resolve_env_suffix(service.vault_agent_container, env)
        app_names = [_resolve_env_suffix(name, env) for name in service.app_containers]
        observations["services"][service.id] = {
            "dokploy_env": env_text,
            "token_lookup": token_lookup,
            "rendered_env": _remote_secret_file_state(vps_host, vault_agent_name),
            "vault_agent_logs": _remote_container_logs(vps_host, vault_agent_name),
            "vault_agent_container": _remote_container_state(
                vps_host, vault_agent_name
            ),
            "app_containers": [
                _remote_container_state(vps_host, name) for name in app_names
            ],
        }
    return observations


def inventory_compose_paths(path: Path | str = DEFAULT_INVENTORY_PATH) -> set[str]:
    return {service.compose_path for service in load_inventory(path)}


def discover_vault_agent_compose_paths(root: Path = REPO_ROOT) -> set[str]:
    paths: set[str] = set()
    for compose_path in root.rglob("compose*.yaml"):
        if "compose-with-vault.yaml" in str(compose_path):
            continue
        text = compose_path.read_text(encoding="utf-8")
        if re.search(r"(?m)^  vault-agent:", text):
            paths.add(str(compose_path.relative_to(root)))
    return paths


def _result(
    service: VaultService,
    check_id: str,
    status: str,
    severity: str,
    summary: str,
    evidence: dict[str, Any],
) -> CheckResult:
    return CheckResult(
        service_id=service.id,
        check_id=check_id,
        status=status,
        severity=severity,
        summary=summary,
        evidence=evidence,
    )


def _is_secret_key(key: str) -> bool:
    key_lower = key.lower()
    return any(secret_key in key_lower for secret_key in SECRET_KEYS)


def _looks_like_vault_token(token: str) -> bool:
    return len(token) >= 16 and not any(ch.isspace() for ch in token)


def _safe_excerpt(logs: str, limit: int = 500) -> str:
    excerpt = logs[-limit:]
    for key in SECRET_KEYS:
        excerpt = re.sub(
            rf"(?i)({key}[A-Z0-9_ -]*[:=])[^\s]+",
            r"\1***REDACTED***",
            excerpt,
        )
    return excerpt


def _resolve_env_suffix(value: str, env: str) -> str:
    suffix = "" if env == "production" else f"-{env}"
    return value.replace("${ENV_SUFFIX}", suffix)


def _vault_addr_from_env(env_vars: dict[str, str | None]) -> str | None:
    if env_vars.get("VAULT_ADDR"):
        return env_vars["VAULT_ADDR"]
    if env_vars.get("INTERNAL_DOMAIN"):
        return f"https://vault.{env_vars['INTERNAL_DOMAIN']}"
    return None


def _ssh(host: str, command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ControlMaster=auto",
            "-o",
            "ControlPersist=60s",
            "-o",
            "ControlPath=/tmp/infra2-vault-audit-%r@%h:%p",
            f"root@{host}",
            command,
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def _remote_json(host: str, command: str) -> dict[str, Any]:
    result = _ssh(host, command)
    if result.returncode != 0 or not result.stdout.strip():
        return {
            "exists": False,
            "error": result.stderr.strip() or result.stdout.strip(),
        }
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"exists": False, "error": result.stdout.strip()}


def _remote_container_state(host: str, container_name: str) -> dict[str, Any]:
    command = (
        "docker inspect "
        "--format '{{json .}}' "
        f"{shlex.quote(container_name)} 2>/dev/null"
    )
    data = _remote_json(host, command)
    if not data.get("exists", True):
        return {"name": container_name, "exists": False, "error": data.get("error")}
    state = data.get("State", {})
    mounts = [mount.get("Destination") for mount in data.get("Mounts", [])]
    health = state.get("Health", {}).get("Status") or "none"
    return {
        "name": container_name,
        "exists": True,
        "state": state.get("Status"),
        "health": health,
        "restart_count": data.get("RestartCount", 0),
        "mounts": mounts,
    }


def _remote_secret_file_state(host: str, vault_agent_container: str) -> dict[str, Any]:
    script = (
        "if [ ! -e /vault/secrets/.env ]; then "
        "printf '{\"exists\":false}'; "
        "elif [ ! -r /vault/secrets/.env ]; then "
        'printf \'{"exists":true,"readable":false}\'; '
        "else "
        "size=$(stat -c %s /vault/secrets/.env) && "
        "mtime=$(stat -c %Y /vault/secrets/.env) && "
        "if grep -q '<no value>' /vault/secrets/.env; then has_no_value=true; "
        "else has_no_value=false; fi && "
        'printf \'{"exists":true,"readable":true,"size":%s,"mtime":%s,"has_no_value":%s}\' '
        '"$size" "$mtime" "$has_no_value"; '
        "fi"
    )
    command = (
        f"docker exec {shlex.quote(vault_agent_container)} sh -lc {shlex.quote(script)}"
    )
    return _remote_json(host, command)


def _remote_container_logs(host: str, container_name: str) -> str:
    result = _ssh(
        host,
        f"docker logs --tail 200 {shlex.quote(container_name)} 2>&1",
    )
    return result.stdout + result.stderr


def write_report(report: dict[str, Any], *, as_json: bool = False) -> str:
    if as_json:
        return json.dumps(report, indent=2, sort_keys=True)
    lines = [
        f"Vault self-refresh audit: {report['status'].upper()}",
        f"Environment: {report['env']}",
    ]
    for result in report["results"]:
        lines.append(
            f"- {result['status'].upper()} {result['severity']} "
            f"{result['service_id']}::{result['check_id']} - {result['summary']}"
        )
    return "\n".join(lines)
