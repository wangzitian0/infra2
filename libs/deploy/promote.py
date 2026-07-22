#!/usr/bin/env python3
"""Internal fixed-compose app deploy backend used by deploy_v2.

This module is not the public deploy surface. ``deploy_v2(service, type, version_ref,
iac_ref)`` resolves the coordinate, enforces the data-lane red lines, and then calls this
backend for a bespoke app's fixed staging/prod composes (``libs.deploy_contract.SERVICES``
— finance_report and, since #500, truealpha/app). The only deploy identity this backend
accepts directly is ``service`` + ``env`` plus a resolved app commit/image ref; the data
lane is derived from ``deploy_env_config.EnvConfig.data_default`` for observability and is
not caller-overridable.

The backend mutates a Dokploy compose's env and triggers a deploy. The Dokploy client is
injected so it is unit-testable without a live control plane. It owns fixed-compose
assembly + trigger + readiness/parity: IAC_CONFIG_HASH cache-bust, static infra keys,
model-override passthrough, Vault-token preflight, rollout wait, and post-deploy
effective-config verification.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from libs.common import infra_domain
from libs.compose_lock import compose_write_lock
from libs.deploy_env_config import app_compose_env_config, otel_env
from libs.deploy_queue import deployment_start_epoch
from tools.deploy_failure_snapshot import emit_failure_snapshot
from tools.openpanel_clients import openpanel_env
from tools.resolve_deploy_ref import resolve_to_sha

# infra2#525: Dokploy deployment records carry no caller-supplied correlation id, so a
# start-timestamp floor (captured just before OUR OWN deploy_compose() call) is the only
# available signal that a "new" record belongs to a DIFFERENT, unrelated trigger rather
# than to us. Allow a small margin for clock skew between this process and Dokploy's
# server rather than excluding on an exact instant.
_CLOCK_SKEW_TOLERANCE_SECONDS = 5


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


def _started_before(deployment: dict, floor_epoch: float) -> bool:
    """True if `deployment` has a parseable start timestamp that is clearly before
    `floor_epoch` (beyond the clock-skew tolerance) — i.e. it cannot be the record our
    own deploy_compose() call produced, because it started before we even called it.

    A record with no parseable startedAt/createdAt/updatedAt is treated as AMBIGUOUS,
    not excluded (returns False) — we only ever use this to rule a record OUT, never to
    rule one in, so an unparseable timestamp preserves prior (pre-infra2#525) behavior
    instead of introducing a new false-negative.
    """
    started = deployment_start_epoch(deployment)
    if started is None:
        return False
    return started < floor_epoch - _CLOCK_SKEW_TOLERANCE_SECONDS


def wait_for_rollout(
    client,
    compose_id: str,
    before_ids: set[str],
    *,
    timeout: int = 600,
    interval: int = 5,
    _sleep=time.sleep,
    _now=time.monotonic,
    min_started_at: float | None = None,
) -> dict:
    """Poll until a NEW Dokploy deployment record reaches a terminal-good status.

    Raises RuntimeError if the new record errors, TimeoutError if none finishes in the
    window. Close cousin of libs.deploy.deployer._wait_for_new_deployment_record, but NOT
    identical: that one returns a bool and treats `running` as success (a health check
    follows it), whereas this owns readiness itself — it keeps polling past `running`
    until a terminal-good status and raises TimeoutError on the deadline. P2 step 5
    reconciles the two pollers (D5) when deploy logic fully lands in infra2.

    min_started_at (infra2#525 finding 2): "new" here means "not in before_ids", which
    is also true of a deployment record produced by a DIFFERENT, unrelated
    deploy_compose() call that lands between our before_ids snapshot and our poll —
    Dokploy gives us no correlation id to disambiguate. When min_started_at is given
    (the epoch just before we called our own deploy_compose()), a "new" record whose own
    start timestamp is clearly earlier is excluded: it cannot be ours. This closes the
    window between the snapshot and our own trigger; a same-compose deploy that starts
    AFTER our trigger but before our first poll is still indistinguishable from our own
    (Dokploy exposes no request/correlation id on deployment records) and is a residual,
    documented risk — see libs/compose_lock.py for the complementary in-process lock
    that keeps our OWN callers from ever producing that overlap.
    """
    deadline = _now() + max(0, timeout)
    while True:
        new = [
            d
            for d in (client.get_compose_deployments(compose_id) or [])
            if _dep_id(d) and _dep_id(d) not in before_ids
        ]
        if min_started_at is not None:
            new = [d for d in new if not _started_before(d, min_started_at)]
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


def assert_approle_creds_present(service: str, client, compose_id: str) -> None:
    """Fail closed if this service's compose uses Vault AppRole auth but the env about
    to be deployed lacks role/secret/addr creds.

    #290/#316 added this exact guard (the #257/#290 foot-gun: an AppRole config change
    lands without VAULT_ROLE_ID/VAULT_SECRET_ID, so the vault-agent crash-loops instead
    of the service ever starting) — but only on the legacy Deployer.composing() path
    and libs.deploy.preview's own copy. This promote path (what deploy_v2 actually
    routes every app staging/prod deploy through) never carried it over, so a fixed
    compose missing AppRole creds deployed a crash-looping vault-agent with no
    preflight signal. update_compose_env merges into the EXISTING compose env and this
    function's caller never sets these keys, so reading the compose's current env here
    reflects exactly what will still be true after this deploy.
    """
    from pathlib import Path

    from libs.service_registry import service_attrs

    meta = service_attrs().get(service)
    if not meta or not meta.compose_path:
        return  # no statically-registered compose file to inspect
    compose_text = Path(meta.compose_path).read_text(encoding="utf-8")
    if "VAULT_ROLE_ID" not in compose_text and "VAULT_SECRET_ID" not in compose_text:
        return  # service does not use AppRole auth

    env_text = client.get_compose_env(compose_id)
    missing = [
        key
        for key in ("VAULT_ROLE_ID", "VAULT_SECRET_ID", "VAULT_ADDR")
        if not (_env_value(env_text, key) or "").strip()
    ]
    if missing:
        raise ValueError(
            f"{service}: compose uses Vault AppRole auth but {', '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} missing from the deploy env — the "
            "vault-agent would crash-loop (missing role/secret) or hang reaching an "
            "empty address (missing VAULT_ADDR) and deadlock on its healthcheck (~6 "
            f"min) instead of starting. Run `invoke vault.setup-approle "
            f"--service={service} --deploy` (or set VAULT_ADDR, e.g. "
            "https://vault.<INTERNAL_DOMAIN>) on the compose/project env before "
            "deploying."
        )


def ensure_generated_secrets(service: str, env: str) -> None:
    """Auto-provision this service's Vault-generated runtime secrets before deploying.

    truealpha#447 root cause: app-web hard-fails on boot without SECRET_KEY
    ("must come from Vault, never the development default"). truealpha's
    Deployer already carries the fix (AppDeployer.ensure_runtime_secrets,
    generates+stores it if missing, idempotent) — but that method is only ever
    invoked from the legacy pre_compose/sync path (invoke <service>.sync via
    the iac-runner). This promote path (what deploy_v2 actually routes every
    app staging/prod deploy through) never called it, so #447 was only fixed
    by a one-off manual Vault seed, not durably: a future Vault wipe/rotation
    for this env would silently ship the same crash-loop again with no
    self-healing. Mirrors assert_approle_creds_present's placement (before any
    compose mutation) but self-heals instead of failing closed — provisioning
    a missing secret is safe to retry/no-op, unlike a hard Vault auth gate.

    Uses libs.deploy.deployer.load_deployer_class (real import, unlike this
    module's other service lookups) because the provisioning logic itself
    — WHICH keys, how they're generated — is app-specific and already lives,
    tested, on each app's own Deployer; duplicating it here as a second
    implementation is exactly the kind of drift this session has been
    finding and removing elsewhere in this repo.
    """
    from libs.deploy.deployer import load_deployer_class

    deployer_cls = load_deployer_class(service)
    if deployer_cls is None:
        return  # no Deployer to consult (e.g. a test double service_id) — nothing to do
    if not deployer_cls.ensure_runtime_secrets(env=env):
        raise ValueError(
            f"{service}: failed to auto-provision one or more runtime secrets in "
            f"Vault for env {env!r} — see the Vault write error logged above."
        )


def preflight_vault_token(client, compose_id: str, *, min_ttl_hours: int = 48):
    """Fail closed before deploy if the compose's legacy VAULT_APP_TOKEN is present but
    invalid or expiring within min_ttl_hours. The token is gated only when present — a
    compose with no VAULT_APP_TOKEN is left alone (not every compose uses Vault).

    AppRole services (VAULT_ROLE_ID/VAULT_SECRET_ID present) are skipped: post-migration a
    vestigial VAULT_APP_TOKEN can linger in Dokploy and expire un-renewed, so gating on it
    would hard-block an AppRole deploy that would otherwise clean it up. Reuses the
    class-free libs.env.verify_vault_token. This does NOT auto-repair; it fails closed.

    No caller-supplied domain: the token being verified lives on the ONE shared Vault
    instance, never a per-service app-routing domain (infra_domain() — #561's general
    form; this call site was the one #561 didn't cover, since it's phrased as a
    positional caller-supplied param rather than a URL built inline).
    """
    from libs.common import infra_domain
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
        token, addr=f"https://vault.{infra_domain()}", min_ttl_hours=min_ttl_hours
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
    libs.deploy.deployer._await_effective_config_hash (compose-id-based rather than by service).
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
    service: str = "finance_report/app",
    staging_validated: bool = False,
    break_glass: bool = False,
    repo: str | None = None,
    image_ref: str | None = None,
    iac_ref: str = "",
    branch: str | None = None,
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

    branch (truealpha#447's root cause): the compose's OWN Dokploy github-source ref —
    re-asserted on every call, mirroring libs.deploy.preview.up's identical re-assert
    ("which also re-assert them on a redeploy"). Without this, a fixed compose's source
    ref is whatever it was set to at creation and NEVER changes again — deploy_v2's
    iac_ref is validated/recorded but never reaches the actual git source Dokploy clones.
    finance_report's staging/prod happened to be created pointing at "main" (so it always
    incidentally tracked HEAD); truealpha's was created pointing at a specific tag from
    #478 (2026-07-11) and silently never advanced past it — every subsequent "successful"
    deploy re-cloned that same 9-day-old commit, so infra2#562's secrets.ctmpl fix (and
    this very re-assert fix) could not reach a running stack until this landed. None when
    omitted (existing callers/tests unaffected); deploy_v2 always passes its clone_ref.

    Also self-heals this service's Vault-generated runtime secrets before any mutation
    (ensure_generated_secrets, truealpha#447's actual fix — the branch re-assert above
    only explains why the symptom persisted after the initial code fix landed; this call
    is what makes the fix durable rather than a one-off manual Vault seed).
    """
    # Explicit None checks: an empty string is a caller error, not a silent fallback.
    if not domain or any(c.isspace() for c in domain):
        raise ValueError(
            f"invalid domain {domain!r}: must be non-empty with no whitespace "
            "(it is interpolated into a line-based compose env file)."
        )
    sha = resolve_to_sha(code, repo=repo) if repo is not None else resolve_to_sha(code)
    cfg = app_compose_env_config(service, env)
    data_lane = cfg.data_default

    if cfg.dynamic:
        raise ValueError(
            f"{env!r} is a per-PR dynamic env with no fixed compose; bind a compose_id "
            "via the preview lifecycle instead of calling deploy() directly."
        )
    if cfg.compose_id is None:
        raise ValueError(
            f"{service!r} has no Dokploy compose registered for env {env!r} "
            "(libs.deploy_env_config._APP_COMPOSE_OVERRIDES) — nothing to deploy to."
        )
    if cfg.requires_staging_first and not staging_validated and not break_glass:
        raise ValueError(
            f"{env!r} requires a staging deploy of digest {sha} first "
            "(promote-not-rebuild). Pass staging_validated=True once staging has run "
            "this exact digest, or break_glass=True as an audited override (H5)."
        )

    # Computed here (not just where identity/openpanel need it below) because
    # ensure_generated_secrets also needs it, before any mutation.
    deploy_environment = {"prod": "production"}.get(env.strip().lower(), env)

    # Fail closed BEFORE any mutation if this compose can't actually authenticate to
    # Vault — unconditional (unlike the legacy VAULT_APP_TOKEN check below), matching
    # the legacy Deployer.composing() path where this has always been a hard gate, not
    # an opt-in.
    assert_approle_creds_present(service, client, cfg.compose_id)

    # Self-heal BEFORE any mutation if this service's Vault-generated runtime secrets
    # (e.g. truealpha's SECRET_KEY, #447) are missing — a fresh/rotated env would
    # otherwise ship a crash-looping app with no signal until it's already deployed.
    ensure_generated_secrets(service, deploy_environment)

    # Fail closed BEFORE any mutation if the compose's Vault token can't carry the deploy.
    # preflight_vault_token resolves the shared Vault host itself now — no domain param
    # to get wrong (#550/#561: this app's own routing domain must never reach it).
    if verify_vault:
        preflight_vault_token(client, cfg.compose_id)

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
    from libs.deploy_contract import service_spec
    from libs.service_identity import ServiceIdentity

    svc_spec = service_spec(service)
    identity = ServiceIdentity.build(
        service,
        deploy_environment,
        component=svc_spec.identity_component,
        service_name=svc_spec.resolved_identity_service_name(),
        version=image_tag,
        iac_ref=iac_ref,
    )
    env_vars.update(identity.deploy_env())
    env_vars["OTEL_SERVICE_NAME"] = identity.service_name
    env_vars["OTEL_RESOURCE_ATTRIBUTES"] = identity.otel_resource_attributes()
    # #372: the finance_report app frontend reads OPENPANEL_CLIENT_ID at runtime
    # (server layout -> <Analytics>). This fixed-compose path never ran the app's
    # pre_compose, so the per-env client id was dropped and analytics never started.
    # Inject it here (single source: tools.openpanel_clients) for the fixed envs
    # this path handles (staging/production); empty for any env without a project.
    env_vars.update(openpanel_env(env))
    # #368: FE OTLP endpoint built ONCE (libs.deploy_env_config.otel_env) and injected
    # here so the compose consumes it instead of re-constructing the otel.<domain> URL.
    # SigNoz ingest is the shared platform instance (infra_domain), never this app's own
    # routing domain — an app-domain override would ship the frontend an otel.<app-domain>
    # endpoint with no collector behind it (#550/#561).
    env_vars.update(otel_env(domain=infra_domain()))
    if model_overrides:
        # only non-empty overrides (an unset override must not blank the running model)
        env_vars.update({k: v for k, v in model_overrides.items() if v})

    # infra2#525: Dokploy's compose.one has no version/etag/updatedAt to gate the write
    # on, and its deployment records carry no caller-supplied correlation id — so the
    # whole read-modify-write-deploy-wait-verify sequence below is serialized per
    # compose_id via an in-process advisory lock. This closes both the lost-update race
    # (a concurrent update_compose_env clobbering this call's env write) and rollout
    # cross-contamination (a concurrent deploy_compose landing a record wait_for_rollout
    # could otherwise mistake for this call's own) for any caller in this process. It
    # does NOT serialize across separate processes/CI runners — see
    # libs/compose_lock.py for what covers that gap today.
    with compose_write_lock(cfg.compose_id):
        # update_compose_env merges with the existing compose env (keeps runtime-injected
        # secrets/AppRole creds); we only override the digest + URL + suffix + infra keys.
        # Snapshot deployment ids BEFORE triggering so wait_for_rollout watches the new one.
        before_ids = (
            _deployment_ids(client.get_compose_deployments(cfg.compose_id))
            if wait
            else set()
        )
        if branch:
            client.update_compose(cfg.compose_id, branch=branch)
        client.update_compose_env(cfg.compose_id, env_vars=env_vars)
        try:
            # Captured immediately before triggering: the earliest wall-clock instant a
            # deployment record OUR OWN call could have produced may carry (infra2#525
            # finding 2 — see wait_for_rollout's min_started_at).
            trigger_epoch = time.time()
            client.deploy_compose(cfg.compose_id)
            if wait:
                wait_for_rollout(
                    client,
                    cfg.compose_id,
                    before_ids,
                    timeout=timeout,
                    min_started_at=trigger_epoch,
                )
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

                # ClickHouse URL resolution lives in deploy_ingestion_smoke (single owner,
                # reusing the round-trip canary's env) — do not re-read it here.
                verify_deploy_ingestion(
                    service_name=identity.service_name,
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
    are dropped by ``deploy`` (only non-empty overrides are applied). Consumed by the
    unified ``deploy_v2`` front door so the promote path threads them identically.
    """
    import os

    return {
        "PRIMARY_MODEL": os.getenv("DEPLOY_PRIMARY_MODEL_OVERRIDE", ""),
        "OCR_MODEL": os.getenv("DEPLOY_OCR_MODEL_OVERRIDE", ""),
        "VISION_MODEL": os.getenv("DEPLOY_VISION_MODEL_OVERRIDE", ""),
    }
