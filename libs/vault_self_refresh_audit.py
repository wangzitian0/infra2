"""Read-only Vault self-refresh audit helpers.

The module is intentionally split into pure classifiers and thin live collection
adapters. Tests exercise the classifiers and inventory/static contracts without
requiring live Vault, Dokploy, or SSH access.

## Recency semantics are mandatory (#531)

This module was hit FOUR separate times by the same defect class: a check
that reads a signal without any notion of *when* it happened, so it
eventually misreports resolved history as a current problem
(``classify_token`` checking a static field against a since-migrated auth
model; ``classify_rendered_env`` using file mtime -- "when did the secret
last change" -- as an "is the agent alive" proxy; ``classify_container``
comparing Docker's lifetime-cumulative ``RestartCount`` with no time bound;
``_remote_container_logs`` tailing 200 lines with no ``--since``, so an
old, resolved crash-loop's log spam can sit in the window indefinitely).
See #531 for the full investigation.

The rule going forward, for every check added to this module: state its
recency semantics explicitly, one of --

1. **Bounded by an explicit time window** -- e.g. "only flag if the most
   recent occurrence was within N seconds of now" (see `libs/recency.py`'s
   `is_recently_flapping`, or a `--since <window>` bound on a `docker logs`
   read). Use this for anything that accumulates or persists across time --
   counters, logs, on-disk file content/mtime, anything answering "how many
   times has X happened" or "does the recent history contain Y".
2. **Explicitly justified as safe to leave unbounded** -- reserved for
   checks that are direct reads of *current* state with no history
   component at all, e.g. "does the container exist right now" / "is the
   container's Docker state currently `running`" / "is the current Health
   status `healthy`". These have no notion of "old" to guard against: there
   is only ever one current value, not an accumulating trail of past ones.

When in doubt, treat it as case 1. The failure mode this guards against is
silent and slow (a check working correctly right up until some accumulated
history stops matching present reality), so it is not something a quick
code read or test run reliably catches after the fact -- decide the recency
story at write time, not later.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import json
import os
import re
import shlex
import subprocess
import time
from typing import Any

from libs.deploy_queue import parse_epoch_seconds
from libs.env import verify_vault_token
from libs.recency import is_recently_flapping


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVENTORY_PATH = REPO_ROOT / "docs/ssot/vault-self-refresh-inventory.yaml"
SECRET_KEYS = ("token", "secret", "password", "key", "authorization")

# (#526) Optional `vault:true` fields that are legitimately allowed to sit
# unprovisioned in Vault: they have a real render line in secrets.ctmpl (so
# tools/validate_required_env.py's wiring check is satisfied), but whether
# Vault actually holds a non-empty VALUE is a separate, deliberate human
# decision about enabling the optional feature the field unlocks. Once the
# render-wiring gap closes, nothing else flags "wired but still empty" -- this
# watchlist is the read-only, informational backstop for exactly that blind
# spot. Not a periodic scan of every vault:true field: only add an entry here
# when a field is both optional-by-architecture *and* outside every other
# probe (DEPENDENCY_MANIFEST startup checks, `_check_static_config`, etc.) --
# see issue #526 for the full audit of finance_report's 19 vault:true fields.
OPTIONAL_INERT_FIELD_WATCHLIST: tuple[tuple[str, str], ...] = (
    # secrets.ctmpl render line landed in #482/PR#520; the Vault value itself
    # is intentionally unset until EPIC-023's DB-backed LLM-provider-secret
    # storage is turned on for finance_report.
    ("finance_report/app", "LLM_ENCRYPTION_KEYS"),
)
# (#531) Docker's RestartCount is lifetime-cumulative; the only other
# restart-relevant signal it exposes is State.StartedAt (when the CURRENT run
# began, i.e. the time of the most recent restart). classify_container()
# therefore only flags a high restart count if the most recent restart was
# itself within this window -- see libs/recency.py's module docstring for the
# full reasoning. Sizing this window is a real tradeoff, not an arbitrary
# pick:
#   - Too short (e.g. minutes) risks missing a real, currently-active
#     crash-loop whose backoff has stretched out, or simply not lining up
#     with when this audit happens to run.
#   - Too long (e.g. a day+) re-widens the exact bug this fixes: a container
#     that restarted many times right at the start of a long-since-resolved
#     incident would stay flagged for the whole window even though nothing
#     has been wrong for most of it.
# 1 hour is deliberately generous relative to a real crash-loop's cadence
# (Docker's restart backoff caps at seconds-to-low-minutes, so a container
# that is STILL actively flapping will restart well inside an hour) while
# being short enough that "stable for 12+ days" (the verified-live
# platform/prefect case) reliably clears it. This audit currently runs on a
# daily CI schedule (#531/PR#532) plus ad hoc manual `invoke
# vault-audit.self-refresh` runs; revisit this downward if the schedule ever
# tightens to sub-hourly.
RESTART_RECENCY_WINDOW_SECONDS = 3600

# (#531) `docker logs` has no notion of "only what's relevant to a live
# health check" -- without a `--since` bound, a long-lived, sparsely-logging
# container's --tail window can still contain a resolved incident's crash-loop
# spam from weeks ago, which classify_vault_agent_logs's substring matching
# would then misattribute to the present. 1h mirrors RESTART_RECENCY_WINDOW_SECONDS's
# reasoning: long enough that a real, currently-unfolding problem's log lines
# are virtually guaranteed to be inside it (this audit's daily schedule means
# a problem that started and got fixed entirely within an hour, days ago,
# reasonably shouldn't still page today), short enough that ancient,
# unrelated incidents fall out of the window entirely. A parameter (not a
# hardcoded constant) since a future caller with a different polling cadence
# (e.g. #475's tighter sidecar loop) may reasonably want a tighter window.
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
    if service.auth_method == "approle":
        # AppRole services (#264/#531) authenticate with VAULT_ROLE_ID + VAULT_SECRET_ID,
        # not a static VAULT_APP_TOKEN -- there is no token to shape-check, look up,
        # confirm renewable, or TTL-gate, so those sub-checks are skipped entirely
        # (mirrors libs/deploy/promote.py::preflight_vault_token's AppRole branch).
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
        # #531: vault-agent's static_secret_render_interval only rewrites this file
        # when the underlying Vault secret's CONTENT changes, not on every poll -- a
        # healthy, low-churn secret can legitimately go long stretches without a
        # re-render. mtime age is therefore "when did the secret last change," not
        # "is vault-agent alive" -- that's already covered by classify_container()'s
        # Docker health status (itself backed by the compose's own `vault token
        # lookup-self` healthcheck). Report informationally, never fail, matching the
        # #526 classify_optional_field_inertness pattern -- this never gates the
        # overall audit status (see audit_from_observations).
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


def optional_inert_fields_for(service_id: str) -> tuple[str, ...]:
    """Watchlisted optional-field names to check for inertness on a service."""
    return tuple(
        field_name
        for sid, field_name in OPTIONAL_INERT_FIELD_WATCHLIST
        if sid == service_id
    )


def classify_optional_field_inertness(
    service: VaultService,
    field_name: str,
    rendered_env_text: str | None,
) -> CheckResult:
    """Report (never fail) whether a watchlisted optional field is populated.

    This is deliberately status="info" always: an empty/missing value here
    means the field's render-wiring is fine (tools/validate_required_env.py
    already gates that) but nobody has provisioned the actual Vault secret --
    an architectural "is this optional feature turned on?" fact, not a
    health/drift defect worth failing the audit over. See #526.
    """
    populated = bool(parse_env(rendered_env_text or "").get(field_name))
    if populated:
        return _result(
            service,
            f"optional-field-inertness::{field_name}",
            "info",
            "P3",
            f"{field_name} is populated (dependent feature active)",
            {"field": field_name, "populated": True},
        )
    return _result(
        service,
        f"optional-field-inertness::{field_name}",
        "info",
        "P3",
        f"{field_name} is unset/empty in the rendered secrets file "
        "(dependent feature is inert)",
        {"field": field_name, "populated": False},
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
    # (#531) restart_count alone is Docker's lifetime-cumulative counter --
    # flag only if it's ALSO recent (derived from State.StartedAt, the start
    # of the current run i.e. the time of the most recent restart). See
    # RESTART_RECENCY_WINDOW_SECONDS above and libs/recency.py for the full
    # reasoning. `started_at` missing/unparseable is treated as "no recency
    # evidence" (last_event_at=0.0 -> effectively infinite age), i.e. does
    # NOT flag -- conservative by design, matching every other unbounded ->
    # bounded fix in this module: absence of a recency signal must never be
    # treated as "recent".
    observed_now = float(now if now is not None else time.time())
    started_at_epoch = parse_epoch_seconds(container_state.get("started_at"))
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
        for field_name in optional_inert_fields_for(service.id):
            results.append(
                classify_optional_field_inertness(
                    service, field_name, str(obs.get("rendered_env_text", ""))
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
        for app_state in obs.get("app_containers", []):
            results.append(
                classify_container(
                    service,
                    app_state,
                    check_id="app-container",
                    expected_mount=service.app_secret_mount_path,
                    now=now,
                )
            )
    # "info" results (e.g. optional-field-inertness, #526) are report-only and
    # never gate the audit's overall pass/fail -- only real health/drift
    # checks do.
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

    `log_since` (#531) bounds how far back `_remote_container_logs` looks --
    see `DEFAULT_LOG_SINCE`'s comment for the reasoning. Exposed as a
    parameter (not hardcoded) since a different caller/cadence may reasonably
    want a different window; this audit's own scheduled/manual callers use
    the default.
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
        # AppRole services (#264/#531) have no static token to look up -- mirrors the
        # skip in classify_token / preflight_vault_token above.
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
        app_names = [_resolve_env_suffix(name, env) for name in service.app_containers]
        inert_fields = optional_inert_fields_for(service.id)
        observations["services"][service.id] = {
            "dokploy_env": env_text,
            "token_lookup": token_lookup,
            "rendered_env": _remote_secret_file_state(vps_host, vault_agent_name),
            # Only fetched for services with OPTIONAL_INERT_FIELD_WATCHLIST
            # entries (#526) -- keeps secret-content exposure scoped to the
            # fields this audit actually needs to see are non-empty.
            "rendered_env_text": (
                _remote_secret_file_text(vps_host, vault_agent_name)
                if inert_fields
                else ""
            ),
            "vault_agent_logs": _remote_container_logs(
                vps_host, vault_agent_name, since=log_since
            ),
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
    """Run a read-only command over SSH against `host`.

    Normally invoked from inside the VPS (the iac-runner container), where root's
    default SSH identity/known_hosts already trust `host`, so no `-i`/`-p` flags are
    needed. A GitHub Actions runner has neither (#531): when the same
    INFRA2_WATCHDOG_SSH_KEY_PATH/_PORT/_USER env vars the route-canary/watchdog jobs
    already provision (see .github/workflows/ops-checks.yml's "Configure SSH key"
    steps) are present, use them explicitly instead of relying on ambient SSH config.
    Absent those env vars, behavior is byte-identical to before this change.
    """
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
        # (#531) Raw ISO-8601 StartedAt string -- the start of the CURRENT
        # run, i.e. the time of the most recent restart (or creation, if
        # never restarted). classify_container() parses this with
        # libs.deploy_queue.parse_epoch_seconds to decide whether a high
        # restart_count is still recent enough to matter. Kept as the raw
        # string here (this is a thin collection adapter -- parsing belongs
        # in the pure classifier, matching classify_rendered_env's mtime
        # handling).
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


def _remote_secret_file_text(host: str, vault_agent_container: str) -> str:
    """Read the raw rendered secrets file content (read-only `cat`).

    Unlike `_remote_secret_file_state`, this returns the actual KEY=VALUE
    content so `classify_optional_field_inertness` (#526) can check specific
    watchlisted fields for emptiness -- `collect_live_observations` only calls
    this for services that have an `OPTIONAL_INERT_FIELD_WATCHLIST` entry, to
    keep secret-content exposure scoped to what the audit actually needs.
    """
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
    """Recent stdout+stderr, time-bounded by `since` (a `docker logs
    --since`-compatible duration like "1h").

    (#531) `--tail 200` alone has no notion of *when* those 200 lines were
    written -- a long-lived, sparsely-logging container's tail window can
    still contain a resolved incident's crash-loop spam from weeks ago, which
    `classify_vault_agent_logs`'s substring matching would then misattribute
    to the present. `--since` bounds that; `--tail 200` is kept as a
    belt-and-suspenders cap on volume within the window, not the recency
    control. See DEFAULT_LOG_SINCE's comment for how the default was picked.
    """
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
