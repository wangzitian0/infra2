#!/usr/bin/env python3
"""Internal fixed-compose app deploy backend used by deploy_v2.

This module is not the public deploy surface. ``deploy_v2(service, type, version_ref,
iac_ref)`` resolves the coordinate, enforces the data-lane red lines, and then calls this
backend for the finance_report app's fixed staging/prod composes. The only deploy
identity this backend accepts directly is ``env`` plus a resolved app commit/image ref;
the data lane is derived from ``deploy_env_config.EnvConfig.data_default`` for
observability and is not caller-overridable.

The backend mutates a Dokploy compose's env and triggers a deploy. The Dokploy client is
injected so it is unit-testable without a live control plane. It owns fixed-compose
assembly + trigger + readiness/parity: IAC_CONFIG_HASH cache-bust, static infra keys,
model-override passthrough, Vault-token preflight, rollout wait, and post-deploy
effective-config verification.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass

from tools.deploy_env_config import env_config, otel_env
from tools.deploy_failure_snapshot import emit_failure_snapshot
from tools.openpanel_clients import openpanel_env
from tools.resolve_deploy_ref import resolve_to_sha


@dataclass(frozen=True)
class DeployPlan:
    env: str
    sha: str
    compose_id: str
    data: str  # derived data_lane from EnvConfig, kept for deploy_v2 result detail
    env_vars: dict[str, str]


def _dep_id(deployment: dict) -> str:
    return str(deployment.get("deploymentId") or deployment.get("id") or "")


def _deployment_ids(deployments) -> set[str]:
    return {_dep_id(d) for d in (deployments or []) if _dep_id(d)}


def wait_for_rollout(
    client,
    compose_id: str,
    before_ids: set[str],
    *,
    timeout: int = 600,
    interval: int = 5,
    _sleep=time.sleep,
    _now=time.monotonic,
) -> dict:
    """Poll until a NEW Dokploy deployment record reaches a terminal-good status.

    Raises RuntimeError if the new record errors, TimeoutError if none finishes in the
    window. Close cousin of libs.deployer._wait_for_new_deployment_record, but NOT
    identical: that one returns a bool and treats `running` as success (a health check
    follows it), whereas this owns readiness itself — it keeps polling past `running`
    until a terminal-good status and raises TimeoutError on the deadline. P2 step 5
    reconciles the two pollers (D5) when deploy logic fully lands in infra2.
    """
    deadline = _now() + max(0, timeout)
    while True:
        new = [
            d
            for d in (client.get_compose_deployments(compose_id) or [])
            if _dep_id(d) and _dep_id(d) not in before_ids
        ]
        for d in new:
            status = str(d.get("status") or "").lower()
            if status == "error":
                raise RuntimeError(
                    f"deploy rollout entered error (compose {compose_id})"
                )
            if status in {"done", "success", "successful"}:
                return d
        if _now() >= deadline:
            raise TimeoutError(
                f"deploy rollout did not finish within {timeout}s (compose {compose_id})"
            )
        _sleep(max(1, interval))


def _env_value(env_str: str, key: str) -> str | None:
    """Read one KEY=VALUE from a Dokploy compose env blob (same simple line format
    update_compose_env writes). Returns None if absent."""
    for line in (env_str or "").split("\n"):
        line = line.strip()
        if line.startswith(f"{key}=") and not line.startswith("#"):
            return line.split("=", 1)[1].strip()
    return None


def preflight_vault_token(
    client, compose_id: str, domain: str, *, min_ttl_hours: int = 48
):
    """Fail closed before deploy if the compose's legacy VAULT_APP_TOKEN is present but
    invalid or expiring within min_ttl_hours. The token is gated only when present — a
    compose with no VAULT_APP_TOKEN is left alone (not every compose uses Vault).

    AppRole services (VAULT_ROLE_ID/VAULT_SECRET_ID present) are skipped: post-migration a
    vestigial VAULT_APP_TOKEN can linger in Dokploy and expire un-renewed, so gating on it
    would hard-block an AppRole deploy that would otherwise clean it up. Reuses the
    class-free libs.env.verify_vault_token. This does NOT auto-repair; it fails closed.
    """
    from libs.env import verify_vault_token

    env_text = client.get_compose_env(compose_id)
    if _env_value(env_text, "VAULT_ROLE_ID") and _env_value(
        env_text, "VAULT_SECRET_ID"
    ):
        return  # AppRole auth -> any leftover VAULT_APP_TOKEN is unused; do not gate on it
    token = _env_value(env_text, "VAULT_APP_TOKEN")
    if not token:
        return  # no token in this compose env -> nothing to gate on
    result = verify_vault_token(
        token, addr=f"https://vault.{domain}", min_ttl_hours=min_ttl_hours
    )
    if not result.get("valid"):
        raise RuntimeError(
            f"VAULT_APP_TOKEN preflight failed for compose {compose_id}: "
            f"{result.get('error') or 'invalid token'}. This is a legacy static token; "
            "remove it from the service's Dokploy env (services authenticate via AppRole now)."
        )


def verify_effective_config_hash(
    client,
    compose_id: str,
    expected_hash: str,
    *,
    timeout: int = 600,
    interval: int = 5,
    _sleep=time.sleep,
    _now=time.monotonic,
) -> str:
    """Poll the compose's effective IAC_CONFIG_HASH until it matches expected_hash, then
    return it; raise RuntimeError if it never advances within the window. Always returns
    the matched hash on success (never None) — a non-advance is an error, not a value.

    This is the post-deploy "did the config actually roll out" gate. Dokploy applies the
    env update asynchronously, so the effective hash can briefly lag the deploy call;
    polling avoids a false-stale verdict on that settling delay while still failing
    closed if it never advances. A transient read error is tolerated like a non-match and
    retried — polling gives each read an independent chance to clear a Dokploy blip; only
    if no clean read ever lands is the last error surfaced. Mirrors
    libs.deployer._await_effective_config_hash (compose-id-based rather than by service).
    """
    deadline = _now() + max(0, timeout)
    last_value: str | None = None
    last_error: Exception | None = None
    while True:
        try:
            last_value = _env_value(
                client.get_compose_env(compose_id), "IAC_CONFIG_HASH"
            )
            last_error = None
        except Exception as exc:  # transient Dokploy read; tolerate within window
            last_error = exc
        if last_value == expected_hash:
            return last_value
        if _now() >= deadline:
            if last_error is not None and last_value is None:
                raise RuntimeError(
                    f"post-deploy config verify could not read effective config for "
                    f"compose {compose_id}: {last_error}"
                )
            raise RuntimeError(
                f"post-deploy config verify failed: effective IAC_CONFIG_HASH "
                f"{last_value!r} never advanced to {expected_hash!r} for compose "
                f"{compose_id} within {timeout}s (deploy may not have taken)."
            )
        _sleep(max(1, interval))


def deploy(
    env: str,
    code: str,
    *,
    domain: str,
    client,
    staging_validated: bool = False,
    break_glass: bool = False,
    repo: str | None = None,
    image_ref: str | None = None,
    wait: bool = False,
    timeout: int = 600,
    model_overrides: dict[str, str] | None = None,
    verify_vault: bool = False,
    verify_config: bool = False,
    verify_ingestion: bool = False,
    _now=time.time,
) -> DeployPlan:
    """Deploy a resolved app commit to a fixed app environment.

    code  -> resolve_deploy_ref (main / vX.Y.Z / <sha>) -> a commit sha.
    env   -> deploy_env_config (which compose, URL, suffix, default data).
    data  -> derived from the env's data_default; callers cannot override it here.

    Gating:
    - The dynamic preview env has no fixed compose; bind one via the preview lifecycle.
    - An env that requires-staging-first (prod) refuses to deploy a digest that has not
      been validated on staging (promote-not-rebuild), unless break_glass=True — an
      audited override (H5). staging_validated is supplied by the caller that checked a
      staging deploy of this exact sha ran.

    When wait=True, block until a new Dokploy deployment record for this compose reaches
    a terminal-good status (raising on rollout error / timeout) — the readiness gate the
    App-repo bash primitive used to own. Default False keeps the pure-assembly callers
    (and the unit tests) side-effect-free beyond the env-update + deploy trigger.

    Parity with the bash dokploy_deploy.sh this replaces (P2 step 4b):
    - IAC_CONFIG_HASH=deploy-<sha>-<ts> is always set as a cache-bust so a same-digest
      promote (staging->prod, promote-not-rebuild) is never a Dokploy no-op.
    - the static infra keys (COMPOSE_PROFILES/TRAEFIK_ENABLE/INTERNAL_DOMAIN) are
      re-asserted against drift.
    - model_overrides (PRIMARY_MODEL/OCR_MODEL/VISION_MODEL, supplied by the staging-E2E
      caller) are merged in when present.
    - verify_vault=True gates the deploy on the compose's VAULT_APP_TOKEN TTL (>=48h).
    - verify_config=True confirms post-deploy that the effective IAC_CONFIG_HASH advanced
      to the one we pushed (fail-closed on a deploy that did not take).
    The verify_* flags default False so the assembly unit tests need no live Dokploy/Vault;
    the CLI enables them so a real workflow deploy gets the full bash-equivalent behavior.
    """
    # Explicit None checks: an empty string is a caller error, not a silent fallback.
    if not domain or any(c.isspace() for c in domain):
        raise ValueError(
            f"invalid domain {domain!r}: must be non-empty with no whitespace "
            "(it is interpolated into a line-based compose env file)."
        )
    sha = resolve_to_sha(code, repo=repo) if repo is not None else resolve_to_sha(code)
    cfg = env_config(env)
    data_lane = cfg.data_default

    if cfg.dynamic:
        raise ValueError(
            f"{env!r} is a per-PR dynamic env with no fixed compose; bind a compose_id "
            "via the preview lifecycle instead of calling deploy() directly."
        )
    if cfg.requires_staging_first and not staging_validated and not break_glass:
        raise ValueError(
            f"{env!r} requires a staging deploy of digest {sha} first "
            "(promote-not-rebuild). Pass staging_validated=True once staging has run "
            "this exact digest, or break_glass=True as an audited override (H5)."
        )

    # Fail closed BEFORE any mutation if the compose's Vault token can't carry the deploy.
    if verify_vault:
        preflight_vault_token(client, cfg.compose_id, domain)

    # The registry publishes images under the 7-char short sha (`git rev-parse --short`,
    # and the promote path's `${sha:0:7}`); pushing the full 40-char sha as IMAGE_TAG
    # would make Dokploy pull a tag that was never published. The canonical full sha
    # stays in DeployPlan.sha as the commit identity; only the image-addressable env
    # carries the short form (GIT_COMMIT_SHA matches what CI bakes as a build-arg).
    # IMAGE_TAG is whatever ref the artifact is PUBLISHED under: a release pulls its
    # retained tag (image_ref="vX.Y.Z"), code pulls the short sha. image_ref is supplied by
    # the resolver (resolve_image_ref); fall back to sha[:7] for direct/legacy callers.
    image_tag = image_ref or sha[:7]
    # IAC_CONFIG_HASH is a per-deploy cache-bust: it changes every call so a same-digest
    # promote still forces a real redeploy (promote-not-rebuild must never no-op).
    # Millisecond resolution so two deploys to the same compose within the same wall
    # second (e.g. a retry) still differ — whole-second granularity could collide and
    # re-introduce the very no-op this guards against.
    config_hash = f"deploy-{image_tag}-{int(_now() * 1000)}"
    env_vars = {
        "IMAGE_TAG": image_tag,
        "GIT_COMMIT_SHA": image_tag,
        "NEXT_PUBLIC_APP_URL": cfg.app_url(domain=domain),
        "ENV_SUFFIX": cfg.env_suffix,
        "ENV_DOMAIN_SUFFIX": cfg.env_suffix,
        "COMPOSE_PROFILES": "app",
        "TRAEFIK_ENABLE": "true",
        "INTERNAL_DOMAIN": domain,
        "IAC_CONFIG_HASH": config_hash,
    }
    # #372: the finance_report app frontend reads OPENPANEL_CLIENT_ID at runtime
    # (server layout -> <Analytics>). This fixed-compose path never ran the app's
    # pre_compose, so the per-env client id was dropped and analytics never started.
    # Inject it here (single source: tools.openpanel_clients) for the fixed envs
    # this path handles (staging/production); empty for any env without a project.
    env_vars.update(openpanel_env(env))
    # #368: FE OTLP endpoint built ONCE (tools.deploy_env_config.otel_env) and injected
    # here so the compose consumes it instead of re-constructing the otel.<domain> URL.
    env_vars.update(otel_env(domain=domain))
    if model_overrides:
        # only non-empty overrides (an unset override must not blank the running model)
        env_vars.update({k: v for k, v in model_overrides.items() if v})

    # update_compose_env merges with the existing compose env (keeps runtime-injected
    # secrets/AppRole creds); we only override the digest + URL + suffix + infra keys.
    # Snapshot deployment ids BEFORE triggering so wait_for_rollout watches the new one.
    before_ids = (
        _deployment_ids(client.get_compose_deployments(cfg.compose_id))
        if wait
        else set()
    )
    client.update_compose_env(cfg.compose_id, env_vars=env_vars)
    try:
        client.deploy_compose(cfg.compose_id)
        if wait:
            wait_for_rollout(client, cfg.compose_id, before_ids, timeout=timeout)
        # Confirm the effective config advanced to what we pushed (deploy actually took).
        if verify_config:
            verify_effective_config_hash(
                client, cfg.compose_id, config_hash, timeout=timeout
            )
        # Opt-in: prove the just-deployed service.version actually ingests into SigNoz
        # (logs+traces), not merely that the rollout/config advanced. This is the
        # platform-side home of the deployed-version ingestion proof — the app emits
        # OTLP and is backend-agnostic; infra2 owns confirming it landed. Default OFF
        # because it depends on Docker-network ClickHouse access + telemetry flush
        # timing; enable per-workflow once validated for the env.
        if verify_ingestion:
            from tools.deploy_ingestion_smoke import verify_deploy_ingestion

            deploy_environment = {"prod": "production"}.get(env.strip().lower(), env)
            verify_deploy_ingestion(
                clickhouse_url=os.getenv(
                    "SIGNOZ_CLICKHOUSE_URL", "http://platform-clickhouse:8123"
                ),
                service_name="finance-report-backend",
                environment=deploy_environment,
                expected_version=image_tag,
            )
    except Exception:
        # #768: on a failed rollout/verify, surface the platform-layer state (Dokploy
        # compose status, latest deployment error, platform-vs-app failure domain) into
        # the step summary so triage separates a platform failure from an app failure
        # without SSH. Best-effort — emit_failure_snapshot never raises — then re-raise
        # the original deploy error unchanged.
        emit_failure_snapshot(client, cfg.compose_id)
        raise

    return DeployPlan(
        env=env, sha=sha, compose_id=cfg.compose_id, data=data_lane, env_vars=env_vars
    )


def model_overrides_from_env() -> dict[str, str]:
    """Model overrides supplied via ``DEPLOY_*_MODEL_OVERRIDE`` env vars.

    The staging-E2E promotion path sets these to pin which models prod runs; empty values
    are dropped by ``deploy`` (only non-empty overrides are applied). Shared by this CLI and
    the unified ``deploy_v2`` front door so both threads them identically.
    """
    import os

    return {
        "PRIMARY_MODEL": os.getenv("DEPLOY_PRIMARY_MODEL_OVERRIDE", ""),
        "OCR_MODEL": os.getenv("DEPLOY_OCR_MODEL_OVERRIDE", ""),
        "VISION_MODEL": os.getenv("DEPLOY_VISION_MODEL_OVERRIDE", ""),
    }


def main(argv: list[str] | None = None) -> int:
    """Retired direct CLI for the internal fixed-compose backend.

    Operational deploys must use ``python -m tools.deploy_v2`` so the full coordinate,
    data-lane red lines, and service routing are enforced before this backend runs.
    """
    parser = argparse.ArgumentParser(
        description="internal fixed-compose deploy backend"
    )
    parser.add_argument("--env", required=True, help="staging | prod (not preview)")
    parser.add_argument("--code", required=True, help="main | vX.Y.Z | <sha>")
    parser.add_argument(
        "--domain", required=True, help="base domain, e.g. zitian.party"
    )
    parser.add_argument(
        "--repo", default=None, help="git remote to resolve code against"
    )
    parser.add_argument(
        "--staging-validated",
        action="store_true",
        help="assert this exact digest passed staging (required for prod)",
    )
    parser.add_argument(
        "--break-glass",
        action="store_true",
        help="audited override of the staging-first gate (H5)",
    )
    parser.add_argument(
        "--no-wait", action="store_true", help="do not block on rollout"
    )
    parser.add_argument("--timeout", type=int, default=600, help="rollout wait seconds")
    parser.add_argument(
        "--skip-vault-check",
        action="store_true",
        help="skip the VAULT_APP_TOKEN TTL preflight (default: on)",
    )
    parser.add_argument(
        "--no-verify-config",
        action="store_true",
        help="skip the post-deploy effective-config check (default: on)",
    )
    parser.add_argument(
        "--allow-internal-direct-call",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    if not args.allow_internal_direct_call:
        print(
            "deploy_primitive CLI is retired; use python -m tools.deploy_v2 so the "
            "deploy_v2 coordinate and derived data_lane red lines are enforced.",
            file=sys.stderr,
        )
        return 2

    # Model overrides come from the same env the staging-E2E workflow already exports,
    # so switching the caller to this CLI needs no new wiring (parity with the bash).
    model_overrides = model_overrides_from_env()

    # Imported lazily so importing the primitive (and its unit tests) needs no Dokploy creds.
    from libs.dokploy import get_dokploy

    try:
        plan = deploy(
            args.env,
            args.code,
            domain=args.domain,
            client=get_dokploy(),
            staging_validated=args.staging_validated,
            break_glass=args.break_glass,
            repo=args.repo,
            wait=not args.no_wait,
            timeout=args.timeout,
            model_overrides=model_overrides,
            verify_vault=not args.skip_vault_check,
            verify_config=not args.no_verify_config,
        )
    except (ValueError, RuntimeError, TimeoutError) as exc:
        print(f"deploy failed: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "env": plan.env,
                "sha": plan.sha,
                "compose_id": plan.compose_id,
                "data_lane": plan.data,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
