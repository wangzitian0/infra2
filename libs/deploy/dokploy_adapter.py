"""Dokploy API integration adapter for service deployment.

Part of libs.deploy domain decomposition (#955, #1009).
This module communicates with Dokploy API endpoints and tracks deployment records.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from infra2_sdk.routing import resolve_dokploy_domains

from libs.deploy.rollout import (
    TERMINAL_SUCCESS_STATUSES,
    RolloutError,
    wait_for_deployment,
)

logger = logging.getLogger(__name__)

_CLOCK_SKEW_TOLERANCE_SECONDS = 5
_TERMINAL_SUCCESS_STATUSES = TERMINAL_SUCCESS_STATUSES


def deployment_ids(deployments: list[dict]) -> set[str]:
    """Extract deployment identifiers from a list of deployment records."""
    return {
        str(deployment.get("deploymentId") or deployment.get("id") or "")
        for deployment in deployments
        if deployment.get("deploymentId") or deployment.get("id")
    }


def get_compose_deployments(client: Any, compose_id: str) -> list[dict]:
    """Retrieve compose deployment records from Dokploy client."""
    get_fn = getattr(client, "get_compose_deployments", None)
    if callable(get_fn):
        try:
            deployments = get_fn(compose_id)
        except Exception:  # noqa: BLE001
            deployments = client.get_compose(compose_id).get("deployments")
    else:
        deployments = client.get_compose(compose_id).get("deployments")
    return (
        [d for d in deployments if isinstance(d, dict)]
        if isinstance(deployments, list)
        else []
    )


def started_before_trigger(
    deployment: dict,
    floor_epoch: float,
    *,
    start_epoch_fn: Callable[[dict], float | None] | None = None,
    clock_skew_tolerance: int = _CLOCK_SKEW_TOLERANCE_SECONDS,
) -> bool:
    """Return true if deployment started before the trigger floor timestamp."""
    if start_epoch_fn is None:
        return False
    started = start_epoch_fn(deployment)
    if started is None:
        return False
    return started < floor_epoch - clock_skew_tolerance


def wait_for_new_deployment_record(
    client: Any,
    compose_id: str,
    previous_ids: set[str],
    timeout_seconds: int,
    interval_seconds: int,
    *,
    min_started_at: float | None = None,
    start_epoch_fn: Callable[[dict], float | None] | None = None,
    terminal_success_statuses: frozenset[str] = _TERMINAL_SUCCESS_STATUSES,
) -> bool:
    """Wait for new deployment record to finish with a terminal status.

    Returns true on success. Returns false when no record appears before deadline.
    Raises RuntimeError on error status or timeout with in-progress record.
    """
    filter_fn = (
        (
            lambda d: started_before_trigger(
                d, min_started_at, start_epoch_fn=start_epoch_fn
            )
        )
        if min_started_at is not None
        else None
    )
    try:
        result = wait_for_deployment(
            lambda: get_compose_deployments(client, compose_id),
            previous_ids,
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
            require_terminal=True,
            terminal_success_statuses=terminal_success_statuses,
            raise_on_error=True,
            raise_on_timeout=False,
            is_filtered_fn=filter_fn,
        )
    except RolloutError as exc:
        raise RuntimeError("Dokploy deployment record entered error") from exc

    if result.status == "done":
        return True

    if result.status == "timeout":
        if result.deployment:
            dep_id = str(
                result.deployment.get("deploymentId")
                or result.deployment.get("id")
                or ""
            )
            dep_status = str(result.deployment.get("status") or "unknown")
            raise RuntimeError(
                f"Dokploy deployment {dep_id} is still "
                f"'{dep_status}' after {timeout_seconds}s; not re-triggering. "
                "Raise DOKPLOY_DEPLOYMENT_RECORD_TIMEOUT_SECONDS if this Dokploy "
                "is slow (#629)."
            )
        return False

    return False


def deploy_compose_with_record_check(
    client: Any,
    compose_id: str,
    *,
    timeout_seconds: int = 180,
    interval_seconds: int = 2,
    wait_fn: Callable[..., bool] | None = None,
    start_epoch_fn: Callable[[dict], float | None] | None = None,
    log_warning: Callable[[str], None] | None = None,
) -> None:
    """Trigger deploy and verify Dokploy creates a deployment record.

    Retries with redeploy_compose if deploy_compose produces no record.
    """
    warn = log_warning or logger.warning
    waiter = wait_fn or (
        lambda c, cid, prev, to, iv, min_s: wait_for_new_deployment_record(
            c, cid, prev, to, iv, min_started_at=min_s, start_epoch_fn=start_epoch_fn
        )
    )

    before_ids = deployment_ids(get_compose_deployments(client, compose_id))
    trigger_epoch = time.time()
    client.deploy_compose(compose_id)
    if waiter(
        client, compose_id, before_ids, timeout_seconds, interval_seconds, trigger_epoch
    ):
        return

    warn(
        "Dokploy deploy did not produce a new deployment record; retrying with compose.redeploy"
    )
    before_ids = deployment_ids(get_compose_deployments(client, compose_id))
    trigger_epoch = time.time()
    client.redeploy_compose(compose_id)
    if waiter(
        client, compose_id, before_ids, timeout_seconds, interval_seconds, trigger_epoch
    ):
        return

    raise RuntimeError(
        "Dokploy deploy/redeploy did not produce a new deployment record; "
        "runtime may still be running stale code"
    )


def prune_stale_dokploy_domains(
    client: Any,
    compose_id: str,
    *,
    log_info: Callable[[str], None] | None = None,
    log_warning: Callable[[str], None] | None = None,
) -> None:
    """Delete Dokploy-attached domains that reference non-existent services."""
    if not hasattr(client, "delete_domain"):
        return
    info_log = log_info or logger.info
    warn_log = log_warning or logger.warning
    domains: list[dict] = []
    if hasattr(client, "_request"):
        try:
            res = client._request("GET", f"domain.byComposeId?composeId={compose_id}")
            if isinstance(res, list):
                domains = [d for d in res if isinstance(d, dict)]
        except Exception:  # noqa: BLE001
            domains = []
    if not domains and hasattr(client, "get_compose"):
        try:
            comp = client.get_compose(compose_id)
            if isinstance(comp, dict):
                raw = comp.get("domains")
                if isinstance(raw, list):
                    domains = [d for d in raw if isinstance(d, dict)]
        except Exception:  # noqa: BLE001
            domains = []
    for d in domains:
        d_id = d.get("domainId")
        if d_id:
            try:
                info_log(
                    f"Pruning stale Dokploy domain attachment: {d.get('host')} "
                    f"(serviceName={d.get('serviceName')})"
                )
                client.delete_domain(d_id)
            except Exception as err:  # noqa: BLE001
                warn_log(
                    f"Failed to delete stale Dokploy domain {d.get('host')}: {err}"
                )


def find_remote_compose(
    client: Any,
    service: str,
    project_name: str,
    env_name: str = "production",
    legacy_names: tuple[str, ...] = (),
) -> dict | None:
    """Find Dokploy compose record by current service name or legacy names."""
    existing = client.find_compose_by_name(service, project_name, env_name=env_name)
    if not existing:
        for legacy_name in legacy_names:
            existing = client.find_compose_by_name(
                legacy_name, project_name, env_name=env_name
            )
            if existing:
                break
    return existing


def upsert_github_compose(
    client: Any,
    *,
    service_name: str,
    project_name: str,
    env_id: str,
    github_id: str,
    repository: str,
    owner: str,
    branch: str,
    compose_path: str,
    effective_env: str,
    existing: dict | None,
    raw_env_str: str,
    log_info: Callable[[str], None] | None = None,
) -> str:
    """Create or update a Dokploy GitHub compose service and return compose_id."""
    info_log = log_info or logger.info
    if existing:
        compose_id = existing["composeId"]
        info_log("Updating existing compose service")
        client.update_compose(
            compose_id,
            source_type="github",
            githubId=github_id,
            repository=repository,
            owner=owner,
            branch=branch,
            composePath=compose_path,
            env=effective_env,
            autoDeploy=False,
        )
    else:
        info_log("Creating new compose service with GitHub provider")
        result = client.create_compose(
            environment_id=env_id,
            name=service_name,
            app_name=f"{project_name}-{service_name}",
            source_type="github",
            githubId=github_id,
            repository=repository,
            owner=owner,
            branch=branch,
            composePath=compose_path,
            env=effective_env,
            autoDeploy=False,
        )
        compose_id = result["composeId"]
        client.update_compose(
            compose_id,
            source_type="github",
            githubId=github_id,
            repository=repository,
            owner=owner,
            branch=branch,
            composePath=compose_path,
            env=raw_env_str,
            autoDeploy=False,
        )
    return compose_id


def parse_remote_config_identity(existing: dict | None) -> dict[str, str | None]:
    """Parse configuration identity from Dokploy compose env string."""
    if not existing:
        return {
            "runtime_hash": None,
            "source_hash": None,
            "deploy_ref": None,
            "identity_schema": None,
            "managed_by": None,
            "service_id": None,
            "environment": None,
        }
    env_str = existing.get("env", "")
    values: dict[str, str] = {}
    for line in env_str.split("\n"):
        key, separator, value = line.partition("=")
        if separator and key in {
            "IAC_CONFIG_HASH",
            "IAC_SOURCE_CONFIG_HASH",
            "IAC_DEPLOY_REF",
            "INFRA_IDENTITY_SCHEMA",
            "INFRA_MANAGED_BY",
            "INFRA_SERVICE_ID",
            "INFRA_ENVIRONMENT",
        }:
            values[key] = value.strip()
    return {
        "runtime_hash": values.get("IAC_CONFIG_HASH"),
        "source_hash": values.get("IAC_SOURCE_CONFIG_HASH"),
        "deploy_ref": values.get("IAC_DEPLOY_REF"),
        "identity_schema": values.get("INFRA_IDENTITY_SCHEMA"),
        "managed_by": values.get("INFRA_MANAGED_BY"),
        "service_id": values.get("INFRA_SERVICE_ID"),
        "environment": values.get("INFRA_ENVIRONMENT"),
    }


def await_effective_config_hash(
    read_hash_fn: Callable[[], str | None],
    expected_hash: str,
    timeout_seconds: int,
    interval_seconds: int,
) -> str | None:
    """Poll Dokploy effective IAC_CONFIG_HASH until it matches expected hash."""
    deadline = time.monotonic() + timeout_seconds
    interval = max(1, interval_seconds)
    last_value: str | None = None
    last_error: Exception | None = None
    while True:
        try:
            last_value = read_hash_fn()
            last_error = None
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        if last_value == expected_hash:
            return last_value
        if time.monotonic() >= deadline:
            if last_value is None and last_error is not None:
                raise last_error
            return last_value
        time.sleep(interval)


def ensure_compose_domains(
    client: Any,
    compose_id: str,
    *,
    env: dict[str, str],
    route_pref: Any = None,
    subdomain: str | None = None,
    service_port: int | str | None = None,
    service_name: str | None = None,
    service_domain_fn: Callable[[str, dict[str, str]], str | None] | None = None,
    log_info: Callable[[str], None] | None = None,
    log_warning: Callable[[str], None] | None = None,
    log_success: Callable[[str], None] | None = None,
) -> dict:
    """Configure Dokploy routing domains for a compose service."""
    info_log = log_info or logger.info
    warn_log = log_warning or logger.warning
    ok_log = log_success or logger.info

    if route_pref is not None:
        domain = env.get("INTERNAL_DOMAIN")
        if not domain:
            warn_log("Domain configuration skipped: INTERNAL_DOMAIN missing")
            return {"created": 0, "skipped": 0, "conflicts": [], "errors": []}
        effective_env = env.get("ENV", "production")
        specs = resolve_dokploy_domains(
            route_pref, tier=effective_env, base_domain=domain
        )
        desired_domains = [asdict(s) for s in specs]
        if desired_domains:
            for d in desired_domains:
                info_log(f"Ensuring domain: https://{d['host']}{d.get('path', '')}")
            result = client.ensure_domains(
                compose_id=compose_id,
                desired_domains=desired_domains,
            )
            if result.get("created", 0) > 0:
                ok_log(f"Configured {result['created']} domain(s) in Dokploy")
            if result.get("conflicts"):
                for c in result["conflicts"]:
                    warn_log(
                        f"Domain conflict: {c['host']} exists with port {c['existing_port']}, need {c['desired_port']}"
                    )
            return result

    if subdomain and service_port:
        domain_host = (
            service_domain_fn(subdomain, env)
            if service_domain_fn
            else (
                f"{subdomain}.{env.get('INTERNAL_DOMAIN')}"
                if env.get("INTERNAL_DOMAIN")
                else None
            )
        )
        if not domain_host:
            warn_log("Domain configuration skipped: INTERNAL_DOMAIN missing")
            return {"created": 0, "skipped": 0, "conflicts": [], "errors": []}
        info_log(f"Ensuring domain: {domain_host}")
        desired_domains = [{"host": domain_host, "port": service_port, "https": True}]
        result = client.ensure_domains(
            compose_id=compose_id,
            desired_domains=desired_domains,
            service_name=service_name,
        )
        if result.get("created", 0) > 0:
            ok_log(f"Domain configured: https://{domain_host}")
        elif result.get("skipped", 0) > 0:
            info_log(f"Domain already configured: {domain_host}")
        if result.get("conflicts"):
            for c in result["conflicts"]:
                warn_log(
                    f"Domain conflict: {c['host']} exists with port {c['existing_port']}, need {c['desired_port']}"
                )
        return result

    return {"created": 0, "skipped": 0, "conflicts": [], "errors": []}
