"""Data models, inventory derivation from SecretsFacet, and pure classifiers for Vault self-refresh audit."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import functools
import re
import subprocess
import time
from typing import Any

from common.core.constants import REPO_ROOT
from common.deploy.queue import parse_epoch_seconds


SECRET_KEYS = ("token", "secret", "password", "key", "authorization")

# (#531) Docker's RestartCount is lifetime-cumulative; the only other
# restart-relevant signal it exposes is State.StartedAt (when the CURRENT run
# began, i.e. the time of the most recent restart). classify_container()
# therefore only flags a high restart count if the most recent restart was
# itself within this window -- see infra2_sdk.observability.recency for the full reasoning.
RESTART_RECENCY_WINDOW_SECONDS = 3600

# (#531) docker logs has no notion of "only what's relevant to a live
# health check" -- without a --since bound, a long-lived, sparsely-logging
# container's --tail window can still contain a resolved incident's crash-loop
# spam from weeks ago.
DEFAULT_LOG_SINCE = "1h"

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
    # (#531/#542) app_containers that by design run WITHOUT the secrets mount
    # (${ENV_SUFFIX} placeholders, resolved per audit env) -- declared on the
    # owning service's SecretsFacet, formerly the MOUNT_EXEMPT_CONTAINERS const.
    mount_exempt_containers: tuple[str, ...] = ()
    # A preview alias stack (common.security.registry marks it preview).
    ephemeral: bool = False

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


def vault_path_template(project: str, service: str) -> str:
    """Build the expected Vault KV path template for a service from project and service names."""
    return f"secret/data/{project}/{{env}}/{service}"


def load_inventory() -> list[VaultService]:
    """Derive the audit inventory deterministically from Deployer SecretsFacet declarations."""
    from common.core.registry import bootstrap_facet_attrs, service_attrs

    metas = {**bootstrap_facet_attrs(), **service_attrs()}
    services: list[VaultService] = []
    seen: dict[str, str] = {}
    for declaring_id in sorted(metas):
        meta = metas[declaring_id]
        for facet in meta.secrets:
            service = _vault_service_from_facet(meta, facet)
            if service.id in seen:
                raise ValueError(
                    f"duplicate vault inventory id {service.id!r} derived from "
                    f"both {seen[service.id]} and {declaring_id}"
                )
            seen[service.id] = declaring_id
            services.append(service)
    if not services:
        raise ValueError(
            "derived vault self-refresh inventory is EMPTY -- the SecretsFacet "
            "registry walk found nothing, which is never a valid state"
        )
    return services


def _hand_written_template(compose_path: str) -> str:
    """The ``secrets.ctmpl`` path that a hand-written stack mounts."""
    return f"{compose_path.rsplit('/', 1)[0]}/secrets.ctmpl"


def _vault_service_from_facet(meta, facet) -> VaultService:
    """Build one VaultService instance from a SecretsFacet and its declaring ServiceMeta."""
    compose_path = facet.compose_path or meta.compose_path
    if not compose_path:
        raise ValueError(
            f"{meta.service_id}: SecretsFacet needs a compose_path (the Deployer "
            "declares none and the facet does not override it)"
        )
    compose_dir = compose_path.rsplit("/", 1)[0]
    # An x-infra2 stack ships its agent config and template inside the compiled compose
    # (infra2#1822), so the compose file is the release source of both.
    agent_config, template = (
        (compose_path, compose_path)
        if _compose_path_is_x_infra2(compose_path)
        else (f"{compose_dir}/vault-agent.hcl", _hand_written_template(compose_path))
    )
    return VaultService(
        id=facet.service_id or meta.service_id,
        project=meta.project,
        dokploy_service=meta.service,
        compose_path=compose_path,
        vault_agent_config_path=agent_config,
        secret_template_path=template,
        vault_path_template=facet.vault_path_template
        or vault_path_template(meta.project, meta.service),
        vault_agent_container=facet.vault_agent_container,
        app_containers=tuple(facet.app_containers),
        vault_token_env_key=facet.vault_token_env_key,
        rendered_secret_path=facet.rendered_secret_path,
        app_secret_mount_path=facet.app_secret_mount_path,
        max_rendered_secret_age_seconds=facet.max_rendered_secret_age_seconds,
        min_token_ttl_hours=facet.min_token_ttl_hours,
        auth_method=facet.auth_method,
        mount_exempt_containers=tuple(facet.mount_exempt_containers),
        ephemeral=_is_preview_stack(compose_dir),
    )


@functools.lru_cache(maxsize=1)
def _preview_stack_dirs() -> frozenset[str]:
    """The stack directories the registry declares as previews (source_env set)."""
    from common.security.registry import SERVICES

    return frozenset(service.directory for service in SERVICES if service.preview)


def _is_preview_stack(compose_dir: str) -> bool:
    return compose_dir in _preview_stack_dirs()


def inventory_ids_not_in_production() -> frozenset[str]:
    """Inventory ids with no production deployment yet -- DERIVED, not declared
    twice (#542, replacing the hand-kept NOT_YET_IN_PRODUCTION constant).
    """
    from common.deploy.env_config import services_without_prod_compose
    from common.core.registry import bootstrap_facet_attrs, service_attrs

    metas = {**bootstrap_facet_attrs(), **service_attrs()}
    owners = services_without_prod_compose() | {
        service_id for service_id, meta in metas.items() if meta.not_yet_in_production
    }
    excluded: set[str] = set()
    for service_id, meta in metas.items():
        if service_id not in owners:
            continue
        excluded.add(service_id)
        excluded.update(facet.service_id for facet in meta.secrets if facet.service_id)
    return frozenset(excluded)


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
    if service.auth_method == "approle":
        missing = [key for key in service.auth_env_keys if not env.get(key)]
        if missing:
            return _result(
                service,
                "dokploy-env-approle",
                "fail",
                "P0",
                f"{', '.join(missing)} missing from Dokploy env",
                {"env_keys": sorted(env.keys()), "missing": missing},
            )
        return _result(
            service,
            "dokploy-env-approle",
            "pass",
            "P0",
            f"{', '.join(service.auth_env_keys)} present in Dokploy env "
            "(AppRole auth; no static token to look up, renew, or TTL-check)",
            {"env_keys": sorted(env.keys())},
        )
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
        token_hint = token[:3] + "***" + token[-3:] if len(token) > 6 else "***"
        return _result(
            service,
            "dokploy-env-token",
            "fail",
            "P0",
            f"{service.vault_token_env_key} is malformed",
            {"token_hint": token_hint, "token_length": len(token)},
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


PREVIEW_STACK_SKIPPED = (
    "preview alias stack: its containers carry a per-alias suffix (-branch-main, -pr-N) "
    "this fixed-environment audit cannot resolve, so it is not measured here; the preview "
    "lifecycle verifies each alias on deploy"
)


def _preview_stack_skipped(service: VaultService) -> CheckResult:
    return _result(
        service,
        "preview-stack",
        "info",
        "P3",
        PREVIEW_STACK_SKIPPED,
        {"compose_path": service.compose_path},
    )


def classify_deployed_template(
    service: VaultService,
    deployed_sha256: str,
    deployed_form: str = "",
    served_ref: str = "",
) -> CheckResult:
    """Is the template the agent has mounted the one this release ships?

    ``deployed_form`` is the form the container shows (``compiled`` or
    ``hand-written``; empty when unknown). ``served_ref`` is the release tag that
    the environment serves (empty when unknown). Main can be ahead of that tag. When
    the container differs from main (form or hash), the check compares the
    container with ``served_ref``. It fails when the container matches neither.
    """
    release_sha256 = _release_template_sha256(service)
    # An x-infra2 stack ships the template inside its compiled compose (infra2#1822).
    release_compiled = _compose_is_x_infra2(service)
    release_form = "compiled" if release_compiled else "hand-written"
    release_source = (
        f"the compiled {service.compose_path} template"
        if release_compiled
        else service.secret_template_path
    )
    evidence = {
        "template_path": release_source,
        "deployed_form": deployed_form,
        "release_form": release_form,
        "deployed_sha256": deployed_sha256[:12],
        "release_sha256": release_sha256[:12],
    }
    served = _served_template(service, served_ref) if served_ref else None
    # The served release replaces main only when its hash is known; an unknown
    # hash (a compiled stack at the tag) keeps the strict comparison with main.
    served_comparable = (
        served is not None and served[0] == deployed_form and bool(served[1])
    )
    if deployed_form and deployed_form != release_form:
        if served_comparable:
            return _classify_served(
                service, deployed_sha256, served_ref, served, evidence
            )
        return _result(
            service,
            "deployed-template",
            "fail",
            "P1",
            f"the vault-agent container runs a {deployed_form} template, but this "
            f"release ships {release_source}; the release never reached the container",
            evidence,
        )
    if release_compiled and not release_sha256:
        from common.deploy.compose_compile import VAULT_TEMPLATE_LABEL

        return _result(
            service,
            "deployed-template",
            "fail",
            "P1",
            f"{release_source} has no {VAULT_TEMPLATE_LABEL} label; the compiler "
            "output cannot prove the template it ships",
            evidence,
        )
    if not deployed_sha256 or not release_sha256:
        return _result(
            service,
            "deployed-template",
            "info",
            "P3",
            f"could not compare {release_source} "
            f"({'deployed' if not deployed_sha256 else 'release'} copy unreadable)",
            evidence,
        )
    if deployed_sha256 != release_sha256:
        if served_comparable:
            # Main can change a template after the served release (#1937).
            return _classify_served(
                service, deployed_sha256, served_ref, served, evidence
            )
        return _result(
            service,
            "deployed-template",
            "fail",
            "P1",
            f"the template in the vault-agent container is not {release_source}, which "
            "this release ships; a template change never reached the container (#628)",
            evidence,
        )
    return _result(
        service,
        "deployed-template",
        "pass",
        "P1",
        f"the template in the vault-agent container matches {release_source}",
        evidence,
    )


def _served_template(service: VaultService, ref: str) -> tuple[str, str, str] | None:
    """``(form, sha256, source)`` of the template of ``service`` at ``ref``.

    The sha256 is known only for a hand-written stack (the ``secrets.ctmpl`` bytes).
    None when git cannot read the compose file at ``ref``.
    """
    import hashlib

    from common.deploy.compose_compile import has_x_infra2
    from common.deploy.config_drift import contents_at_ref

    template_path = _hand_written_template(service.compose_path)
    paths = [service.compose_path, template_path]
    try:
        files = contents_at_ref(ref, paths)
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None
    compose = files.get(service.compose_path)
    if compose is None:
        return None
    if has_x_infra2(compose.decode("utf-8", "replace")):
        return "compiled", "", f"the compiled {service.compose_path} template"
    template = files.get(template_path)
    sha256 = hashlib.sha256(template).hexdigest() if template is not None else ""
    return "hand-written", sha256, template_path


def _classify_served(
    service: VaultService,
    deployed_sha256: str,
    ref: str,
    served: tuple[str, str, str],
    evidence: dict[str, str],
) -> CheckResult:
    """Compare the container with the served release, whose hash is known."""
    _form, served_sha256, source = served
    evidence = {**evidence, "served_ref": ref, "served_sha256": served_sha256[:12]}
    if deployed_sha256 != served_sha256:
        return _result(
            service,
            "deployed-template",
            "fail",
            "P1",
            f"the template in the vault-agent container is not {source} at {ref}, "
            "the served release; a template change never reached the container",
            evidence,
        )
    return _result(
        service,
        "deployed-template",
        "pass",
        "P1",
        f"the template in the vault-agent container matches {source} at {ref}, "
        "the served release; main is ahead of it",
        evidence,
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
            "info",
            "P3",
            f"{service.rendered_secret_path} has not been rewritten in {age}s "
            "(secret content likely unchanged since; vault-agent health is tracked "
            "separately by the container healthcheck)",
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
    now: int | float | None = None,
    restart_recency_window_seconds: int = RESTART_RECENCY_WINDOW_SECONDS,
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
    max_restart_count = int(container_state.get("max_restart_count", 3))
    observed_now = float(now if now is not None else time.time())
    started_at_epoch = parse_epoch_seconds(container_state.get("started_at"))
    from infra2_sdk.observability import is_recently_flapping

    if is_recently_flapping(
        event_count=restart_count,
        last_event_at=started_at_epoch if started_at_epoch is not None else 0.0,
        now=observed_now,
        count_threshold=max_restart_count,
        recency_window_seconds=restart_recency_window_seconds,
    ):
        restart_age = (
            int(max(0.0, observed_now - started_at_epoch))
            if started_at_epoch is not None
            else None
        )
        return _result(
            service,
            check_id,
            "fail",
            "P1",
            f"container {name} restart count is high and recent (still flapping"
            + (
                f"; last restart {restart_age}s ago)"
                if restart_age is not None
                else ")"
            ),
            {**container_state, "restart_age_seconds": restart_age},
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


def _served_ref(env: str) -> str:
    """The release tag that ``env`` serves; empty when it is unknown.

    Only production has a marker tag (``production/vX.Y.Z``).
    """
    if env != "production":
        return ""
    from common.deploy.config_drift import _production_target_tag

    try:
        return _production_target_tag()
    except (SystemExit, OSError, subprocess.CalledProcessError):
        return ""


def audit_from_observations(
    services: list[VaultService],
    observations: dict[str, Any],
    *,
    env: str,
    now: int | None = None,
) -> dict[str, Any]:
    results: list[CheckResult] = []
    observed_services = observations.get("services", {})
    served_ref = _served_ref(env)
    for service in services:
        if service.ephemeral:
            results.append(_preview_stack_skipped(service))
            continue
        obs = observed_services.get(service.id, {})
        env_text = str(obs.get("dokploy_env", ""))
        lookup = obs.get("token_lookup")
        results.append(classify_token(service, env_text, lookup))
        results.append(classify_rendered_env(service, obs.get("rendered_env", {}), now))
        results.append(
            classify_deployed_template(
                service,
                str(obs.get("deployed_template_sha256", "")),
                str(obs.get("deployed_template_form", "")),
                served_ref,
            )
        )
        results.append(
            classify_vault_agent_logs(service, str(obs.get("vault_agent_logs", "")))
        )
        vault_agent_state = obs.get("vault_agent_container", {})
        results.append(
            classify_container(
                service,
                vault_agent_state,
                check_id="vault-agent-container",
                now=now,
            )
        )
        mount_exempt = {
            _resolve_env_suffix(name, env) for name in service.mount_exempt_containers
        }
        for app_state in obs.get("app_containers", []):
            exempt = str(app_state.get("name") or "") in mount_exempt
            results.append(
                classify_container(
                    service,
                    app_state,
                    check_id="app-container",
                    expected_mount=None if exempt else service.app_secret_mount_path,
                    now=now,
                )
            )
    status = (
        "pass" if all(item.status in ("pass", "info") for item in results) else "fail"
    )
    return {
        "schema_version": 1,
        "env": env,
        "status": status,
        "generated_at": int(now if now is not None else time.time()),
        "results": [redact(result.to_dict()) for result in results],
    }


def inventory_compose_paths() -> set[str]:
    return {service.compose_path for service in load_inventory()}


def discover_vault_agent_compose_paths(root: Path = REPO_ROOT) -> set[str]:
    """Every compose file whose deployed stack runs a ``vault-agent`` sidecar.

    A hand-written file declares the sidecar as a service. An ``x-infra2`` file does not:
    the compiler adds it for ``x-infra2.vault`` (infra2#1822 Slice 2).
    """
    from common.deploy.compose_compile import compiles_a_vault_sidecar

    paths: set[str] = set()
    for compose_path in root.rglob("compose*.yaml"):
        if any(part.startswith(".") for part in compose_path.relative_to(root).parts):
            continue
        text = compose_path.read_text(encoding="utf-8")
        if compiles_a_vault_sidecar(text):
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
            rf'(?i)({key}[A-Z0-9_ -]*[:=]\s*["\']?)(?:Bearer\s+)?[^\s"\']+',
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


def _compose_path_is_x_infra2(compose_path: str) -> bool:
    """True when the repository file ``compose_path`` is an ``x-infra2`` source."""
    from common.deploy.compose_compile import has_x_infra2

    compose = REPO_ROOT / compose_path
    return compose.is_file() and has_x_infra2(compose.read_text(encoding="utf-8"))


def _compose_is_x_infra2(service: VaultService) -> bool:
    """True when the stack compose file of ``service`` is an ``x-infra2`` source."""
    return _compose_path_is_x_infra2(service.compose_path)


def _release_template_sha256(service: VaultService) -> str:
    """The SHA-256 of the Vault template that this release ships.

    A hand-written stack ships ``secrets.ctmpl`` on disk. An ``x-infra2`` stack ships
    the template inside its compiled compose, and the compiler writes its SHA-256 to
    the sidecar label ``infra2.vault.template.sha256`` (infra2#1822).
    """
    import hashlib

    if _compose_is_x_infra2(service):
        from common.deploy.compose_compile import load_stack, vault_template_label

        return vault_template_label(load_stack(service.compose_path).compiled)
    path = REPO_ROOT / service.secret_template_path
    if not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()
