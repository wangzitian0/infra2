"""Deployment preflight guards and credential checks.

Consolidates duplicate token and AppRole verification from libs.deploy.deployer
and libs.deploy.promote onto infra2_sdk.secrets, plus GitHub ref reachability
and registry image dependency checks.
"""

from __future__ import annotations

import logging
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from infra2_sdk.secrets import vault_token_status

from libs.deploy.contract import DeployTarget, is_tag_only_iac_env
from libs.deploy.env_config import env_config

_INFRA2_REPO = "https://github.com/wangzitian0/infra2"
_DEFAULT_IMAGE_WAIT_SECONDS = 300.0
_DEFAULT_IMAGE_POLL_SECONDS = 10.0
_RATE_LIMIT_MAX_WAIT_SECONDS = 120
_RATE_LIMIT_DEFAULT_WAIT_SECONDS = 15
_IMAGE_MANIFEST_ACCEPT = (
    "application/vnd.docker.distribution.manifest.list.v2+json, "
    "application/vnd.docker.distribution.manifest.v2+json, "
    "application/vnd.oci.image.manifest.v1+json, "
    "application/vnd.oci.image.index.v1+json"
)


def parse_env_var(env_text: str | None, key: str) -> str | None:
    """Read one KEY=VALUE from a Dokploy compose env string.

    Returns None if absent or empty. Comments starting with '#' are ignored.
    """
    for line in (env_text or "").splitlines():
        line = line.strip()
        if line.startswith(f"{key}=") and not line.startswith("#"):
            val = line.split("=", 1)[1].strip()
            return val if val else None
    return None


def extract_vault_token_from_env(env_text: str | None) -> tuple[bool, str | None]:
    """Parse Dokploy compose env string for legacy VAULT_APP_TOKEN.

    Returns:
        (is_approle, token_or_none)
    """
    if not env_text:
        return False, None

    # AppRole services authenticate via VAULT_ROLE_ID/VAULT_SECRET_ID.
    if parse_env_var(env_text, "VAULT_ROLE_ID") and parse_env_var(
        env_text, "VAULT_SECRET_ID"
    ):
        return True, None

    token = parse_env_var(env_text, "VAULT_APP_TOKEN")
    return False, token


def check_approle_creds(compose_text: str, env_text: str | None) -> list[str]:
    """Return missing AppRole credentials if the compose uses AppRole auth."""
    if "VAULT_ROLE_ID" not in compose_text and "VAULT_SECRET_ID" not in compose_text:
        return []

    return [
        key
        for key in ("VAULT_ROLE_ID", "VAULT_SECRET_ID", "VAULT_ADDR")
        if not parse_env_var(env_text, key)
    ]


def verify_token_status(
    token: str,
    vault_addr: str,
    *,
    min_ttl_hours: int = 24,
    verifier: Any = None,
) -> dict:
    """Verify a Vault token via infra2-sdk vault_token_status.

    Returns the contract dict expected by Deployer and promote:
    valid, ttl_hours, renewable, error, details.
    """
    if verifier is not None:
        res = verifier(token, addr=vault_addr, min_ttl_hours=min_ttl_hours)
        if isinstance(res, dict):
            return res
        return {
            "valid": bool(res),
            "ttl_hours": getattr(res, "ttl_hours", -1),
            "renewable": bool(getattr(res, "renewable", False)),
            "error": getattr(res, "error", None),
            "details": getattr(res, "details", ""),
        }

    status = vault_token_status(vault_addr, token, min_ttl_seconds=min_ttl_hours * 3600)
    is_valid = bool(status.valid)
    ttl_h = status.ttl_hours if status.ttl_seconds >= 0 else -1
    err = None if is_valid else (status.error or "invalid token")
    details = f"Token OK (TTL: {ttl_h}h)" if is_valid else f"Token invalid: {err}"
    return {
        "valid": is_valid,
        "ttl_hours": ttl_h,
        "renewable": bool(status.renewable),
        "error": err,
        "details": details,
    }


def _infra2_owner_name(repo: str) -> str:
    """`https://github.com/wangzitian0/infra2[.git]` -> `wangzitian0/infra2`."""
    return repo.rstrip("/").removesuffix(".git").split("github.com/")[-1]


def _rate_limited(resp) -> bool:
    """403/429 whose headers or body say the GitHub budget is spent."""
    if getattr(resp, "status_code", 200) not in (403, 429):
        return False
    headers = getattr(resp, "headers", None) or {}
    if str(headers.get("X-RateLimit-Remaining", "")).strip() == "0" or headers.get(
        "Retry-After"
    ):
        return True
    return "rate limit" in str(getattr(resp, "text", "") or "").lower()


def _rate_limit_wait_seconds(resp, now: float) -> float:
    headers = getattr(resp, "headers", None) or {}
    retry_after = str(headers.get("Retry-After", "")).strip()
    if retry_after.isdigit():
        return max(1.0, float(retry_after))
    reset = str(headers.get("X-RateLimit-Reset", "")).strip()
    if reset.isdigit():
        return max(1.0, float(reset) - now)
    return float(_RATE_LIMIT_DEFAULT_WAIT_SECONDS)


def assert_iac_ref_on_main(
    iac_ref: str,
    deploy_type: str,
    *,
    repo: str = _INFRA2_REPO,
    token: str | None = None,
    transport=httpx.get,
    sleep=time.sleep,
    now=time.time,
    max_attempts: int = 3,
) -> None:
    """Fail-closed: a fixed-env (staging/prod) iac_ref must be an ON-MAIN release tag."""
    if not is_tag_only_iac_env(deploy_type):
        return
    url = (
        f"https://api.github.com/repos/{_infra2_owner_name(repo)}"
        f"/compare/main...{iac_ref.strip()}"
    )
    headers = {"Accept": "application/vnd.github+json"}
    tok = (
        token
        if token is not None
        else (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "").strip()
    )
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    for attempt in range(1, max_attempts + 1):
        resp = transport(url, headers=headers, timeout=30)
        if not _rate_limited(resp):
            break
        wait = _rate_limit_wait_seconds(resp, now())
        if attempt < max_attempts and wait <= _RATE_LIMIT_MAX_WAIT_SECONDS:
            sleep(wait)
            continue
        why = (
            "the call was UNAUTHENTICATED -- export GITHUB_TOKEN (github.token) in the deploy "
            "step; anonymous calls share GitHub's 60/hour budget per runner IP"
            if not tok
            else "the token's GitHub API budget is spent"
        )
        raise RuntimeError(
            f"GitHub compare API rate-limited (HTTP {resp.status_code}) for {url}: {why}; "
            f"budget resets in ~{wait:.0f}s (attempt {attempt}/{max_attempts}, #635)"
        )
    resp.raise_for_status()
    status = (resp.json() or {}).get("status")
    if status not in ("behind", "identical"):
        raise ValueError(
            f"iac_ref {iac_ref!r} is not on infra2 main (compare status={status!r}); "
            f"staging/prod require an on-main release tag (#465 -- the app-side twin of the "
            f"reconcile off-main guard). Re-cut/use a tag on main."
        )


def _env_number(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {raw!r}")
    if value < 0:
        raise ValueError(f"{name} must be non-negative, got {value}")
    return value


def _registry_image_parts(image: str) -> tuple[str, str]:
    registry, sep, repository = image.partition("/")
    if not sep or not registry or not repository:
        raise ValueError(
            f"image repository must include registry and repository path, got {image!r}"
        )
    return registry, repository


def _parse_bearer_authenticate(header: str) -> dict[str, str]:
    scheme, _, rest = header.partition(" ")
    if scheme.lower() != "bearer":
        raise RuntimeError("registry did not return a Bearer authentication challenge")
    params: dict[str, str] = {}
    for item in rest.split(","):
        key, sep, value = item.strip().partition("=")
        if sep:
            params[key] = value.strip().strip('"')
    return params


def _registry_bearer_token(client: httpx.Client, authenticate_header: str) -> str:
    params = _parse_bearer_authenticate(authenticate_header)
    realm = params.get("realm")
    if not realm:
        raise RuntimeError("registry Bearer challenge did not include a token realm")
    token_params = {
        key: value for key in ("service", "scope") if (value := params.get(key))
    }
    response = client.get(realm, params=token_params)
    response.raise_for_status()
    payload = response.json()
    token = payload.get("token") or payload.get("access_token")
    if not token:
        raise RuntimeError("registry token response did not include a token")
    return str(token)


def _image_manifest_exists(
    image: str, image_ref: str, *, client: httpx.Client | None = None
) -> bool:
    """Return whether image:image_ref exists in the registry."""
    registry, repository = _registry_image_parts(image)
    url = f"https://{registry}/v2/{repository}/manifests/{image_ref}"
    headers = {"Accept": _IMAGE_MANIFEST_ACCEPT}

    def request(c: httpx.Client, *, token: str | None = None) -> httpx.Response:
        req_headers = dict(headers)
        if token:
            req_headers["Authorization"] = f"Bearer {token}"
        return c.get(url, headers=req_headers)

    if client is None:
        with httpx.Client(timeout=10.0, follow_redirects=True) as created:
            return _image_manifest_exists(image, image_ref, client=created)

    response = request(client)
    if response.status_code == 401:
        token = _registry_bearer_token(
            client, response.headers.get("www-authenticate", "")
        )
        response = request(client, token=token)
    if 200 <= response.status_code < 300:
        return True
    if response.status_code == 404:
        return False
    if response.status_code in (401, 403):
        raise RuntimeError(
            f"registry refused manifest check for {image}:{image_ref} "
            f"(status {response.status_code})"
        )
    if response.status_code == 429 or response.status_code >= 500:
        raise RuntimeError(
            f"registry manifest check for {image}:{image_ref} is temporarily unavailable "
            f"(status {response.status_code})"
        )
    raise RuntimeError(
        f"registry manifest check for {image}:{image_ref} returned status "
        f"{response.status_code}"
    )


def _wait_for_image_dependencies(
    spec,
    image_ref: str,
    *,
    timeout: float | None = None,
    poll_seconds: float | None = None,
) -> None:
    """Wait until the service's declared image artifacts expose image_ref."""
    repositories = tuple(getattr(spec, "image_repositories", ()) or ())
    if not repositories:
        return
    max_wait = _env_number("DEPLOY_V2_IMAGE_WAIT_SECONDS", _DEFAULT_IMAGE_WAIT_SECONDS)
    interval = _env_number("DEPLOY_V2_IMAGE_POLL_SECONDS", _DEFAULT_IMAGE_POLL_SECONDS)
    if timeout is not None:
        max_wait = float(timeout)
    if poll_seconds is not None:
        interval = float(poll_seconds)
    if not math.isfinite(max_wait) or not math.isfinite(interval):
        raise ValueError("image wait timeout and poll interval must be finite")
    if max_wait < 0 or interval < 0:
        raise ValueError("image wait timeout and poll interval must be non-negative")
    if max_wait > 0 and interval == 0:
        raise ValueError(
            "image poll interval must be positive when image wait is enabled"
        )

    mod = sys.modules.get("tools.deploy_v2")
    manifest_exists_fn = (
        getattr(mod, "_image_manifest_exists", _image_manifest_exists)
        if mod
        else _image_manifest_exists
    )
    sleep_fn = (
        getattr(getattr(mod, "time", None), "sleep", time.sleep) if mod else time.sleep
    )

    deadline = time.monotonic() + max_wait
    last_missing: list[str] = []
    last_errors: list[str] = []
    while True:
        missing: list[str] = []
        errors: list[str] = []
        for image in repositories:
            try:
                if not manifest_exists_fn(image, image_ref):
                    missing.append(image)
            except (RuntimeError, httpx.HTTPError) as exc:
                errors.append(f"{image}: {exc}")
        if not missing and not errors:
            return
        last_missing = missing
        last_errors = errors
        if time.monotonic() >= deadline:
            parts = []
            if last_missing:
                parts.append(
                    "missing " + ", ".join(f"{i}:{image_ref}" for i in last_missing)
                )
            if last_errors:
                parts.append("errors " + "; ".join(last_errors))
            detail = "; ".join(parts) or "unknown registry readiness state"
            raise RuntimeError(
                f"required image artifacts for {spec.key} image_ref {image_ref!r} "
                f"not published after {max_wait:g}s: {detail}"
            )
        sleep_fn(min(interval, max(0.0, deadline - time.monotonic())))


def resolve_data_lane(target: DeployTarget) -> str:
    """The data source for a target -- derived from the env, not a separate input axis."""
    return env_config(target.env).data_default


def enforce_data_lane_red_lines(
    target: DeployTarget, *, code_reviewed: bool | None = None
) -> str:
    """Fail closed on the data red lines, returning the resolved data_lane."""
    data_lane = resolve_data_lane(target)
    if target.env == "prod" and data_lane != "prod":
        raise ValueError(f"env=prod must use prod data, got data_lane={data_lane!r}")
    if data_lane == "prod" and code_reviewed is not True:
        raise ValueError(
            "prod data requires an explicit code-reviewed signal (RL-DATA-1); "
            f"got code_reviewed={code_reviewed!r}"
        )
    if target.service == "finance_report/app" and target.env in ("staging", "prod"):
        mod = sys.modules.get("tools.deploy_v2")
        path_cls = getattr(mod, "Path", Path) if mod else Path
        manifest_path = path_cls("/data/backups/anonymized/manifest.json")
        if manifest_path.exists():
            try:
                import json
                from datetime import datetime, timezone

                m = json.loads(manifest_path.read_text(encoding="utf-8"))
                gen_at = m.get("generated_at")
                if gen_at:
                    dt = datetime.fromisoformat(gen_at.replace("Z", "+00:00"))
                    age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
                    if age_days > 7.0:
                        logging.getLogger("deploy_v2").warning(
                            "Anonymized snapshot for %s is older than 7 days (age=%.1f days)",
                            target.env,
                            age_days,
                        )
            except Exception as exc:
                logging.getLogger("deploy_v2").debug(
                    "Failed to evaluate anonymized snapshot freshness: %s", exc
                )
    return data_lane
