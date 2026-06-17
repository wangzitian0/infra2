"""Single source of truth for finance_report per-env OpenPanel client ids.

OpenPanel uses one project per environment (model B). The client id is a
**non-secret per-env value**, but it must reach the app frontend container's env
so `<Analytics>` renders. There are two deploy paths and both must inject it:

- legacy `libs.deployer` path — `finance_report/.../10.app/deploy.py:pre_compose`
- the live `deploy_v2` fixed-compose path — `tools/deploy_primitive.deploy`

`10.app` is not an importable module name (leading digit), so this map lives
here in `tools/` and both paths import it — keeping one source of truth instead
of a literal duplicated in each (#372).
"""

from __future__ import annotations

# One OpenPanel project per environment. Empty/missing => analytics is a no-op
# (the component config-gates on a non-empty client id).
OPENPANEL_CLIENTS: dict[str, str] = {
    "production": "28bfa625-8751-4424-9514-29c967f77550",
    "staging": "62d5cfe0-2480-4b6e-b76f-8eabbcaf698f",
    # TODO(#375): mint a real "preview" OpenPanel project and replace this placeholder.
    "preview": "00000000-0000-0000-0000-000000000000",
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
