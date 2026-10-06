"""Backward-compatibility shim — the implementation lives in `libs.security.vault_self_refresh_audit`.

Re-exports only. See `libs/README.md` § Backward-Compatibility Shims: never add new
business logic here; it belongs in the domain package this module points at.
"""

from __future__ import annotations

from libs.security.vault_self_refresh_audit import (
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
    collect_live_observations,
    discover_vault_agent_compose_paths,
    inventory_compose_paths,
    inventory_ids_not_in_production,
    load_inventory,
    parse_env,
    redact,
    vault_path_template,
    write_report,
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
    "vault_path_template",
    "write_report",
]
