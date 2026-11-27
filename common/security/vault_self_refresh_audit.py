"""Read-only Vault self-refresh audit live collection adapters and reporting.

Pure classifiers and data models live in ``common.security.vault_audit``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from common.deploy.compose_compile import COMPILED_TEMPLATE_PATH, VAULT_TEMPLATE_LABEL
from common.security.vault_audit import (
    DEFAULT_LOG_SINCE,
    PREVIEW_STACK_SKIPPED,
    VaultService,
    _resolve_env_suffix,
    _vault_addr_from_env,
    audit_from_observations,
    inventory_ids_not_in_production,
    load_inventory,
    parse_env,
)


def verify_vault_token(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Verify a Vault token via common.security.store."""
    from common.security.store import verify_vault_token as _verify_vault_token

    return _verify_vault_token(*args, **kwargs)


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
    from common.core.environ import get_env
    from common.deploy.dokploy_client import get_dokploy

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

        app_names = [_resolve_env_suffix(name, env) for name in service.app_containers]
        app_states = [_remote_container_state(vps_host, name) for name in app_names]

        template_form, template_sha256 = _remote_template_identity(
            vps_host, vault_agent_name
        )
        observations["services"][service.id] = {
            "dokploy_env": env_text,
            "token_lookup": token_lookup,
            "rendered_env": _remote_secret_file_state(vps_host, vault_agent_name),
            "deployed_template_form": template_form,
            "deployed_template_sha256": template_sha256,
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


HAND_WRITTEN_TEMPLATE_PATH = "/etc/vault/secrets.ctmpl"
FORM_COMPILED = "compiled"
FORM_HAND_WRITTEN = "hand-written"


def _remote_template_identity(host: str, vault_agent_container: str) -> tuple[str, str]:
    """``(form, sha256)`` of the template that the vault-agent container reads.

    The container label gives the form, not the checkout and not the file path. A
    release can lag main, and a rollback can run a hand-written stack that main has
    moved to ``x-infra2``. Only a compiled sidecar carries ``VAULT_TEMPLATE_LABEL``.
    A hand-written sidecar can also write ``/vault/secrets.ctmpl`` (from
    ``VAULT_SECRETS_TEMPLATE``), so the path does not give the form.
    A compiled sidecar writes ``/vault/secrets.ctmpl`` with
    ``printf '%s\\n' "$VAULT_SECRETS_TEMPLATE"``, which adds one newline after the
    template text. The release label hashes the template text, so this function
    removes that one newline before it hashes. For a hand-written sidecar the hash
    is the hash of the mounted ``/etc/vault/secrets.ctmpl`` bytes.
    The result is ``("", "")`` when the container or its template file is missing.
    """
    container = shlex.quote(vault_agent_container)
    label_format = shlex.quote(f'{{{{index .Config.Labels "{VAULT_TEMPLATE_LABEL}"}}}}')
    compiled = shlex.quote(COMPILED_TEMPLATE_PATH)
    mounted = shlex.quote(HAND_WRITTEN_TEMPLATE_PATH)
    script = (
        f"label=$(docker inspect --format {label_format} {container}); "
        'case "$label" in '
        f"''|'<no value>') sum=$(docker exec {container} sha256sum {mounted}) "
        f"|| exit 1; printf '{FORM_HAND_WRITTEN}\\n%s\\n' \"${{sum%% *}}\";; "
        f"*) printf '{FORM_COMPILED}\\n'; docker exec {container} cat {compiled} "
        "|| exit 1;; esac"
    )
    result = _ssh(host, f"sh -c {shlex.quote(script)}")
    if result.returncode != 0:
        return "", ""
    form, _, body = result.stdout.partition("\n")
    if form == FORM_COMPILED:
        if not body.endswith("\n"):
            return form, ""
        return form, hashlib.sha256(body[:-1].encode("utf-8")).hexdigest()
    if form == FORM_HAND_WRITTEN and body.strip():
        return form, body.strip()
    return "", ""


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


REPORT_ENV = "VAULT_SELF_REFRESH_AUDIT_REPORT"
CAN_FAIL_BUT_REPORT_INFO = ("deployed-template",)


def run_check(
    env: str = "production",
    *,
    collect_fn: Callable[[list[VaultService], str], dict[str, Any]] | None = None,
    audit_fn: Callable[[list[VaultService], dict[str, Any], str], dict[str, Any]]
    | None = None,
    excluded_fn: Callable[[], set[str]] = inventory_ids_not_in_production,
) -> dict[str, Any]:
    """Collect live observations and classify them (read-only)."""
    services = load_inventory()
    if env == "production":
        excluded = excluded_fn()
        services = [s for s in services if s.id not in excluded]
    observations = (
        collect_fn(services, env)
        if collect_fn is not None
        else collect_live_observations(services, env=env)
    )
    return (
        audit_fn(services, observations, env)
        if audit_fn is not None
        else audit_from_observations(services, observations, env=env)
    )


def confirmed_finding_pairs(report: dict[str, Any]) -> list[tuple[str, str]]:
    """``(identity key, display line)`` per confirmed ``fail`` result (#962)."""
    return [
        (
            f"{r['service_id']}::{r['check_id']}",
            f"- {r['severity']} {r['service_id']}::{r['check_id']} - {r['summary']}",
        )
        for r in report["results"]
        if r["status"] == "fail"
    ]


def unevaluated_keys(report: dict[str, Any]) -> list[str]:
    """Key prefixes of checks that reported ``info`` instead of pass/fail (#962)."""
    return [
        f"{r['service_id']}::{r['check_id']}"
        for r in report["results"]
        if r["status"] == "info" and r["check_id"] in CAN_FAIL_BUT_REPORT_INFO
    ]


def load_report(env: Mapping[str, str]) -> dict[str, Any] | None:
    """Return the JSON report written by ``main_check()``, or None if missing/invalid."""
    path = (env.get(REPORT_ENV) or "").strip()
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_unevaluated(env: Mapping[str, str]) -> list[str]:
    """Return unevaluated prefixes from the last report, or ``[""]`` when unreadable."""
    path = (env.get(REPORT_ENV) or "").strip()
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        if report["status"] == "pass":
            return unevaluated_keys(report)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return [""]


def main_check(*, run_fn: Callable[[str], dict[str, Any]] | None = None) -> int:
    """Run production Vault self-refresh audit and write optional report JSON."""
    report = run_fn("production") if run_fn is not None else run_check("production")
    print(write_report(report))
    if path := os.environ.get(REPORT_ENV, "").strip():
        try:
            Path(path).write_text(json.dumps(report), encoding="utf-8")
        except OSError as exc:
            print(f"could not record the report: {exc}", file=sys.stderr)
    return 0 if report["status"] == "pass" else 1
