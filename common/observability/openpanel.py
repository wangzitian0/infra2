"""Single source of truth for OpenPanel client configuration and query helpers (#840, #1311).

Canonical domain location for per-environment OpenPanel client IDs, compose-env
contributions, and shared OpenPanel export query & funnel summary helpers from
``infra2_sdk.observability``.
"""

from __future__ import annotations

from pathlib import Path

from infra2_sdk.observability import (
    build_openpanel_payload,
    format_openpanel_markdown,
    load_openpanel_funnel_presets,
    post_openpanel_json,
    resolve_openpanel_api_key,
    resolve_openpanel_api_url,
)

OPENPANEL_ANALYTICS_PATH = (
    Path(__file__).resolve().parents[2]
    / "finance_report"
    / "finance_report"
    / "observability"
    / "openpanel_analytics.json"
)

# One OpenPanel project per environment. Empty/missing => analytics is a no-op
# (the component config-gates on a non-empty client id).
OPENPANEL_CLIENTS: dict[str, str] = {
    "production": "28bfa625-8751-4424-9514-29c967f77550",
    "staging": "62d5cfe0-2480-4b6e-b76f-8eabbcaf698f",
    # #375: the "preview" OpenPanel project (project id `finance-preview`,
    # client "preview web") — all preview aliases (main / pr-N / commit-<sha7>)
    # share this one project; alias granularity lives in event attributes.
    "preview": "6e8f9d85-4456-4dcb-bc2b-7b68298f22dd",
}

# The two deploy paths name prod differently: deploy_v2/deploy_primitive uses
# `prod`, the legacy `ENV` uses `production`. Normalize to the map's canonical keys.
_ENV_ALIASES = {"prod": "production"}


def openpanel_env(env_name: str) -> dict[str, str]:
    """Return the OpenPanel compose-env contribution for `env_name`.

    Accepts either naming convention (`prod`/`production`). `OPENPANEL_ENVIRONMENT`
    is the canonical env name, set only when a real client id exists — mirroring the
    frontend's config-gate (no client id => the analytics SDK stays inert).
    """
    canonical = _ENV_ALIASES.get(env_name, env_name)
    client_id = OPENPANEL_CLIENTS.get(canonical, "")
    return {
        "OPENPANEL_CLIENT_ID": client_id,
        "OPENPANEL_ENVIRONMENT": canonical if client_id else "",
    }


def load_estate_openpanel_presets() -> dict[str, tuple[str, ...]]:
    """Load the estate OpenPanel funnel presets from the declarative JSON contract."""
    return load_openpanel_funnel_presets(OPENPANEL_ANALYTICS_PATH)


__all__ = [
    "OPENPANEL_ANALYTICS_PATH",
    "OPENPANEL_CLIENTS",
    "build_openpanel_payload",
    "format_openpanel_markdown",
    "load_estate_openpanel_presets",
    "load_openpanel_funnel_presets",
    "openpanel_env",
    "post_openpanel_json",
    "resolve_openpanel_api_key",
    "resolve_openpanel_api_url",
]
