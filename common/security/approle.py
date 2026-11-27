"""Shared Vault AppRole lifecycle CRUD (single source of truth)."""

from __future__ import annotations

import io
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from common.security.backends import policy_name as vault_policy_name

APPROLE_TOKEN_TTL = "24h"
APPROLE_TOKEN_MAX_TTL = "168h"
_ACCESSOR_RE = re.compile(r"^[0-9A-Za-z-]{1,64}$")


@dataclass(frozen=True)
class AppRoleResult:
    role_name: str
    role_id: str
    secret_id: str
    new_accessor: str | None
    issue_tag: str
    prior_accessors: tuple[str, ...] = ()


def render_policy_hcl(policy_rules: str, env_name: str) -> str:
    """Substitute ``{{env}}`` placeholders in a Vault policy template."""
    return policy_rules.replace("{{env}}", env_name)


def default_approle_policy(project: str, env_name: str, service: str) -> str:
    """Default least-privilege read/list policy for a service AppRole."""
    return (
        f'path "secret/data/{project}/{env_name}/{service}" {{\n'
        f'  capabilities = ["read", "list"]\n}}'
    )


def list_secret_id_accessors(
    c: Any, venv: dict[str, str], role: str
) -> list[str] | None:
    """Return all secret-id accessors of ``role``, or ``None`` if Vault failed."""
    res = c.run(
        f"vault list -format=json auth/approle/role/{role}/secret-id",
        env=venv,
        hide=True,
        warn=True,
    )
    try:
        data = json.loads(res.stdout or "")
    except ValueError:
        return None
    if data == {}:
        return []
    if (
        res.ok
        and isinstance(data, list)
        and all(isinstance(a, str) and _ACCESSOR_RE.match(a) for a in data)
    ):
        return data
    return None


def destroy_secret_ids(
    c: Any,
    venv: dict[str, str],
    role: str,
    accessors: list[str],
    *,
    on_error: Callable[[str], None] | None = None,
    on_success: Callable[[str], None] | None = None,
) -> bool:
    """Destroy each listed secret-id accessor of ``role``."""
    ok = True
    for accessor in accessors:
        if not c.run(
            f"vault write auth/approle/role/{role}/secret-id-accessor/destroy "
            f"secret_id_accessor={accessor}",
            env=venv,
            hide=True,
            warn=True,
        ).ok:
            if on_error:
                on_error(f"   {role}: failed to destroy secret id accessor {accessor}")
            ok = False
    if ok and on_success:
        on_success(f"   {role}: destroyed {len(accessors)} earlier secret id(s)")
    return ok


def list_token_accessors(c: Any, venv: dict[str, str]) -> list[str] | None:
    """Return every token accessor from Vault, or ``None`` when Vault fails."""
    res = c.run(
        "vault list -format=json auth/token/accessors",
        env=venv,
        hide=True,
        warn=True,
    )
    try:
        data = json.loads(res.stdout or "")
    except ValueError:
        return None
    if data == {}:
        return []
    if (
        res.ok
        and isinstance(data, list)
        and all(isinstance(a, str) and _ACCESSOR_RE.match(a) for a in data)
    ):
        return data
    return None


def revoke_earlier_role_tokens(
    c: Any,
    venv: dict[str, str],
    role: str,
    issue_tag: str,
    *,
    on_error: Callable[[str], None] | None = None,
    on_success: Callable[[str], None] | None = None,
) -> bool:
    """Revoke every token of ``role`` whose ``secret_id_issue`` metadata differs."""
    accessors = list_token_accessors(c, venv)
    if accessors is None:
        if on_error:
            on_error(f"   {role}: failed to list token accessors")
        return False
    ok = True
    revoked = 0
    for accessor in accessors:
        lookup = c.run(
            f"vault write -format=json auth/token/lookup-accessor accessor={accessor}",
            env=venv,
            hide=True,
            warn=True,
        )
        if not lookup.ok:
            if "invalid accessor" in (lookup.stderr or "").lower():
                continue
            if on_error:
                on_error(f"   {role}: failed to inspect token accessor {accessor}")
            ok = False
            continue
        try:
            data = json.loads(lookup.stdout or "")["data"]
        except (ValueError, KeyError, TypeError):
            if on_error:
                on_error(f"   {role}: unreadable token lookup for accessor {accessor}")
            ok = False
            continue
        meta = data.get("meta")
        if not isinstance(meta, dict) or meta.get("role_name") != role:
            continue
        if meta.get("secret_id_issue") == issue_tag:
            continue
        if not c.run(
            f"vault write auth/token/revoke-accessor accessor={accessor}",
            env=venv,
            hide=True,
            warn=True,
        ).ok:
            if on_error:
                on_error(f"   {role}: failed to revoke token accessor {accessor}")
            ok = False
        else:
            revoked += 1
    if ok and on_success:
        on_success(f"   {role}: revoked {revoked} earlier token(s)")
    return ok


def reconcile_approle(
    c: Any,
    venv: dict[str, str],
    *,
    project: str,
    env_name: str,
    service: str,
    policy_rules: str,
    issue_credentials: bool = True,
    token_ttl: str = APPROLE_TOKEN_TTL,
    token_max_ttl: str = APPROLE_TOKEN_MAX_TTL,
    list_prior_accessors: Callable[[Any, dict[str, str], str], list[str] | None]
    | None = None,
) -> AppRoleResult:
    """Write policy + AppRole role and optionally issue tagged credentials.

    Callers that rotate live credentials MUST inject/refresh all consumers with the new
    ``(role_id, secret_id)`` BEFORE calling ``destroy_secret_ids`` / ``revoke_earlier_role_tokens``
    against ``result.prior_accessors``.
    """
    role = vault_policy_name(project, env_name, service)
    rendered_policy = render_policy_hcl(policy_rules, env_name)

    res = c.run(
        f"vault policy write {role} -",
        env=venv,
        in_stream=io.StringIO(rendered_policy),
        hide=True,
        warn=True,
    )
    if not res.ok:
        raise RuntimeError(
            f"Failed to write Vault policy {role}: {getattr(res, 'stderr', '')}"
        )

    res = c.run(
        f"vault write auth/approle/role/{role} "
        f"token_policies={role} token_no_default_policy=true "
        f"token_ttl={token_ttl} token_max_ttl={token_max_ttl} "
        f"secret_id_num_uses=0 secret_id_ttl=0",
        env=venv,
        hide=True,
        warn=True,
    )
    if not res.ok:
        raise RuntimeError(
            f"Failed to write Vault AppRole {role}: {getattr(res, 'stderr', '')}"
        )

    if not issue_credentials:
        return AppRoleResult(
            role_name=role,
            role_id="",
            secret_id="",
            new_accessor=None,
            issue_tag="",
            prior_accessors=(),
        )

    accessor_reader = list_prior_accessors or list_secret_id_accessors
    prior_accessors = accessor_reader(c, venv, role)
    if prior_accessors is None:
        raise RuntimeError(f"Failed to list existing secret ids of {role}")

    role_id_res = c.run(
        f"vault read -format=json auth/approle/role/{role}/role-id",
        env=venv,
        hide=True,
        warn=True,
    )
    issue_tag = str(time.time_ns())
    secret_id_res = c.run(
        f"vault write -format=json auth/approle/role/{role}/secret-id "
        f'metadata=\'{{"secret_id_issue":"{issue_tag}"}}\'',
        env=venv,
        hide=True,
        warn=True,
    )
    if not role_id_res.ok or not secret_id_res.ok:
        raise RuntimeError(f"Failed to obtain role-id/secret-id for {role}")

    role_id = json.loads(role_id_res.stdout)["data"]["role_id"]
    issued = json.loads(secret_id_res.stdout)["data"]
    secret_id = issued["secret_id"]
    new_accessor = issued.get("secret_id_accessor")
    return AppRoleResult(
        role_name=role,
        role_id=role_id,
        secret_id=secret_id,
        new_accessor=new_accessor,
        issue_tag=issue_tag,
        prior_accessors=tuple(prior_accessors),
    )


__all__ = [
    "APPROLE_TOKEN_MAX_TTL",
    "APPROLE_TOKEN_TTL",
    "AppRoleResult",
    "default_approle_policy",
    "destroy_secret_ids",
    "list_secret_id_accessors",
    "list_token_accessors",
    "reconcile_approle",
    "render_policy_hcl",
    "revoke_earlier_role_tokens",
]
