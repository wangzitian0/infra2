"""Read-only Vault self-refresh audit helpers and live collection adapters.

Pure classifiers and data models are defined in ``libs.security.vault_audit``;
this module provides live collection adapters and maintains full backward
compatibility by re-exporting all audit interfaces and symbols.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from typing import Any

from libs.core.constants import REPO_ROOT
from libs.security.store import verify_vault_token
from libs.security.vault_audit import (
    CheckResult,
    DEFAULT_LOG_SINCE,
    ERROR_LOG_PATTERNS,
    PREVIEW_STACK_SKIPPED,
    RESTART_RECENCY_WINDOW_SECONDS,
    SECRET_KEYS,
    VaultService,
    audit_from_observations,
    classify_container,
    classify_deployed_template,
    classify_optional_field_inertness,
    classify_rendered_env,
    classify_token,
    classify_vault_agent_logs,
    discover_vault_agent_compose_paths,
    inventory_compose_paths,
    inventory_ids_not_in_production,
    load_inventory,
    parse_env,
    redact,
    vault_path_template,
    _is_preview_stack,
    _is_secret_key,
    _looks_like_vault_token,
    _preview_stack_dirs,
    _preview_stack_skipped,
    _release_template_sha256,
    _resolve_env_suffix,
    _result,
    _safe_excerpt,
    _vault_addr_from_env,
    _vault_service_from_facet,
)

__all__ = [
    "CheckResult",
    "DEFAULT_LOG_SINCE",
    "ERROR_LOG_PATTERNS",
    "PREVIEW_STACK_SKIPPED",
    "RESTART_RECENCY_WINDOW_SECONDS",
    "SECRET_KEYS",
    "VaultService",
    "audit_from_observations",
    "classify_container",
    "classify_deployed_template",
    "classify_optional_field_inertness",
    "classify_rendered_env",
    "classify_token",
    "classify_vault_agent_logs",
    "collect_live_observations",
    "discover_vault_agent_compose_paths",
    "inventory_compose_paths",
    "inventory_ids_not_in_production",
    "load_inventory",
    "parse_env",
    "redact",
    "REPO_ROOT",
    "vault_path_template",
    "verify_vault_token",
    "write_report",
    "_is_preview_stack",
    "_is_secret_key",
    "_looks_like_vault_token",
    "_preview_stack_dirs",
    "_preview_stack_skipped",
    "_release_template_sha256",
    "_resolve_env_suffix",
    "_result",
    "_safe_excerpt",
    "_vault_addr_from_env",
    "_vault_service_from_facet",
]


def collect_live_observations(
    services: list[VaultService],
    *,
    env: str,
    host: str | None = None,
    log_since: str = DEFAULT_LOG_SINCE,
) -> dict[str, Any]:
    """Collect read-only live observations from Dokploy, Vault, and Docker.

    This function intentionally only reads state. It does not restart, mutate,
    renew, or rotate anything.
    """
    from libs.core.environ import get_env
    from libs.deploy.dokploy_client import get_dokploy

    env_vars = get_env(env)
    vps_host = host or env_vars.get("VPS_HOST")
    if not vps_host:
        raise ValueError("VPS_HOST is required for live audit")
    internal_domain = env_vars.get("INTERNAL_DOMAIN")
    vault_addr = _vault_addr_from_env(env_vars)
    dokploy_host = f"cloud.{internal_domain}" if internal_domain else None
    client = get_dokploy(host=dokploy_host)
    observations: dict[str, Any] = {"services": {}}
    for service in services:
        if service.ephemeral:
            observations["services"][service.id] = {"skipped": PREVIEW_STACK_SKIPPED}
            continue
        compose = client.find_compose_by_name(
            service.dokploy_service,
            project_name=service.project,
            env_name=env,
        )
        if compose is None and service.legacy_dokploy_services:
            for legacy_name in service.legacy_dokploy_services:
                compose = client.find_compose_by_name(
                    legacy_name,
                    project_name=service.project,
                    env_name=env,
                )
                if compose is not None:
                    break
        env_text = compose.get("env", "") if compose else ""
        if service.auth_method == "approle":
            token_lookup = None
        else:
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
        agent_state = _remote_container_state(vps_host, vault_agent_name)
        if agent_state.get("status") == "missing" and service.legacy_dokploy_services:
            for legacy_name in service.legacy_dokploy_services:
                candidate_agent = _resolve_env_suffix(
                    service.vault_agent_container.replace(
                        service.dokploy_service, legacy_name
                    ),
                    env,
                )
                candidate_state = _remote_container_state(vps_host, candidate_agent)
                if candidate_state.get("status") != "missing":
                    vault_agent_name = candidate_agent
                    agent_state = candidate_state
                    break

        app_names = [_resolve_env_suffix(name, env) for name in service.app_containers]
        app_states = []
        for name in app_names:
            st = _remote_container_state(vps_host, name)
            if st.get("status") == "missing" and service.legacy_dokploy_services:
                for legacy_name in service.legacy_dokploy_services:
                    candidate_app = _resolve_env_suffix(
                        name.replace(service.dokploy_service, legacy_name),
                        env,
                    )
                    candidate_st = _remote_container_state(vps_host, candidate_app)
                    if candidate_st.get("status") != "missing":
                        st = candidate_st
                        break
            app_states.append(st)

        inert_fields = service.optional_inert_fields
        observations["services"][service.id] = {
            "dokploy_env": env_text,
            "token_lookup": token_lookup,
            "rendered_env": _remote_secret_file_state(vps_host, vault_agent_name),
            "deployed_template_sha256": _remote_template_sha256(
                vps_host, vault_agent_name
            ),
            "rendered_env_text": (
                _remote_secret_file_text(vps_host, vault_agent_name)
                if inert_fields
                else ""
            ),
            "vault_agent_logs": _remote_container_logs(
                vps_host, vault_agent_name, since=log_since
            ),
            "vault_agent_container": agent_state,
            "app_containers": app_states,
        }
    return observations


def _ssh(host: str, command: str) -> subprocess.CompletedProcess[str]:
    """Run a read-only command over SSH against host."""
    args = [
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
    ]
    key_path = os.environ.get("INFRA2_WATCHDOG_SSH_KEY_PATH", "").strip()
    if key_path:
        args += [
            "-i",
            key_path,
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
        ]
    port = os.environ.get("INFRA2_WATCHDOG_SSH_PORT", "").strip()
    if port:
        args += ["-p", port]
    user = os.environ.get("INFRA2_WATCHDOG_SSH_USER", "").strip() or "root"
    args += [f"{user}@{host}", command]
    return subprocess.run(
        args,
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
        "started_at": state.get("StartedAt"),
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


def _remote_template_sha256(host: str, vault_agent_container: str) -> str:
    command = (
        f"docker exec {shlex.quote(vault_agent_container)} "
        "sh -lc 'sha256sum /etc/vault/secrets.ctmpl 2>/dev/null | cut -d\" \" -f1'"
    )
    result = _ssh(host, command)
    return result.stdout.strip() if result.returncode == 0 else ""


def _remote_secret_file_text(host: str, vault_agent_container: str) -> str:
    command = (
        f"docker exec {shlex.quote(vault_agent_container)} "
        "sh -lc 'cat /vault/secrets/.env 2>/dev/null'"
    )
    result = _ssh(host, command)
    if result.returncode != 0:
        return ""
    return result.stdout


def _remote_container_logs(
    host: str, container_name: str, *, since: str = DEFAULT_LOG_SINCE
) -> str:
    result = _ssh(
        host,
        f"docker logs --since {shlex.quote(since)} --tail 200 "
        f"{shlex.quote(container_name)} 2>&1",
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
