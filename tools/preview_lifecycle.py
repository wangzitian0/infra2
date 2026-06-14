#!/usr/bin/env python3
"""Manual lifecycle for the multi-alias PREVIEW environment.

The preview env (deploy_env_config.py) is a *family* of throwaway stacks, one per
alias — ``main`` / ``pr-<N>`` / ``commit-<sha7>`` — each its OWN Dokploy compose with
its OWN ephemeral database (finance_report/finance_report/preview/compose.yaml). This
module is the manual entrypoint that creates, deploys, health-checks, and tears those
stacks down. Unlike staging/prod (a fixed compose driven by deploy_primitive.deploy),
preview is dynamic: the compose for an alias is found-or-created here by a deterministic
name, so any number of aliases coexist and outlive a CI run until explicitly torn down.

Design seams (mirroring deploy_primitive / resolve_deploy_ref):
- code -> sha           : resolve_deploy_ref.resolve_to_sha (the App image tag to pull)
- (kind, value) -> ids  : deploy_env_config.preview_alias (suffix / url / slug / label)
- side effects          : the injected Dokploy client (create/update/deploy/delete)
- readiness             : an injected http getter polls report-<alias>/api/health

Everything with side effects takes an injected client/getter so the orchestration is
unit-testable with a mock — NO live Dokploy/HTTP call happens in tests. The compose
template is pulled from infra2 via Dokploy's GitHub source (sourceType="github",
composePath=...), the same mechanism libs.deployer uses, so the relative vault-agent
files the template mounts (vault-agent.hcl / secrets.ctmpl) exist on the Dokploy host.

CLI:
    python -m tools.preview_lifecycle up   --kind pr --value 5 --code main --domain zitian.party
    python -m tools.preview_lifecycle down --kind pr --value 5 --domain zitian.party
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from tools.deploy_env_config import (
    PREVIEW_ENVIRONMENT,
    PREVIEW_PROJECT,
    PreviewAlias,
    preview_alias,
)
from tools.resolve_deploy_ref import resolve_to_sha

# The infra2 repo + path Dokploy pulls the preview compose template from (same source
# mechanism as libs.deployer; the compose mounts ./vault-agent.hcl + ./secrets.ctmpl,
# which only exist when Dokploy clones the repo rather than uploading a raw blob).
_PREVIEW_COMPOSE_PATH = "finance_report/finance_report/preview/compose.yaml"


@dataclass(frozen=True)
class PreviewResult:
    action: str  # "up" | "down"
    alias: str
    compose_id: str | None
    sha: str | None
    url: str
    healthy: bool | None  # None when no health check was performed (down / --no-wait)


# Which env's Vault app-secrets path a preview reads (AI keys / S3 / OTEL). An ephemeral
# alias has no Vault path of its own, so previews reuse a fixed source env's app secrets;
# the preview secrets.ctmpl reads secret/data/finance_report/<PREVIEW_SECRET_ENV>/app.
_PREVIEW_SECRET_ENV = "staging"

# The source env's app compose to read AppRole creds from (its name in Dokploy).
_SOURCE_APP_COMPOSE = "app"
# The runtime AppRole creds the preview vault-agent logs in with. Preview reads the source
# env's secret path, so it reuses that env's role verbatim — the same creds staging itself
# runs with (injected once by `invoke setup-approle`); see _source_app_vault_creds.
_VAULT_CRED_KEYS = ("VAULT_ADDR", "VAULT_ROLE_ID", "VAULT_SECRET_ID")


def _source_app_vault_creds(client, source_env: str) -> dict[str, str]:
    """Read the AppRole creds the ``source_env`` app compose runs with.

    Previews reuse the source env's AppRole (they read its secret path via
    ``PREVIEW_SECRET_ENV``), exactly the creds staging runs with — set once on the source
    compose by ``invoke setup-approle``. We copy them onto the preview compose at deploy
    time because a throwaway alias is recreated each ``up`` (and deleted on ``down``), so
    it can never rely on a manual one-time injection persisting the way a fixed env does.
    """
    comp = client.find_compose_by_name(
        _SOURCE_APP_COMPOSE, PREVIEW_PROJECT, env_name=source_env
    )
    if not comp:
        raise RuntimeError(
            f"cannot source preview Vault creds: no {_SOURCE_APP_COMPOSE!r} compose in "
            f"{PREVIEW_PROJECT}/{source_env}"
        )
    env = client.get_compose_env(comp["composeId"]) or ""
    creds = {}
    for line in env.splitlines():
        key, _, value = line.partition("=")
        if key in _VAULT_CRED_KEYS and value:
            creds[key] = value
    missing = [k for k in _VAULT_CRED_KEYS if k not in creds]
    if missing:
        raise RuntimeError(
            f"{source_env} {_SOURCE_APP_COMPOSE!r} compose is missing Vault creds "
            f"{missing}; run `invoke setup-approle --project finance_report` first"
        )
    return creds


def _validate_domain(domain: str) -> str:
    """Reject a malformed domain (empty or containing whitespace).

    Whitespace in the domain would corrupt the Dokploy host (cloud.<domain>) and the
    Traefik Host() rules / env blob, so it is rejected up front for BOTH up and down.
    Returns the validated domain unchanged so callers can use the result inline.
    """
    if not domain or any(c.isspace() for c in domain):
        raise ValueError(f"invalid domain {domain!r}: non-empty, no whitespace")
    return domain


def _preview_env_vars(
    alias: PreviewAlias, *, sha: str, domain: str, _now=time.time
) -> dict[str, str]:
    """Assemble the Dokploy compose env for one preview alias.

    Mirrors deploy_primitive.deploy's shared keys (IMAGE_TAG/GIT_COMMIT_SHA short-sha,
    NEXT_PUBLIC_APP_URL, ENV_SUFFIX/ENV_DOMAIN_SUFFIX, COMPOSE_PROFILES, TRAEFIK_ENABLE,
    INTERNAL_DOMAIN, IAC_CONFIG_HASH cache-bust) and adds the preview-only bits: ENV is
    the alias display label (telemetry consumes it), PREVIEW_SECRET_ENV picks which env's
    Vault app secrets to render, and the ephemeral-DB knobs point DATABASE_URL at the
    stack's own local postgres.

    NOTE: VAULT_ADDR / VAULT_ROLE_ID / VAULT_SECRET_ID are NOT set here — they are
    runtime AppRole creds that must be supplied on the Dokploy compose env once (and are
    preserved across redeploys), exactly like staging/prod. The lifecycle never logs or
    embeds them.
    """
    image_tag = sha[:7]  # registry publishes images under the 7-char short sha
    config_hash = f"preview-{image_tag}-{int(_now() * 1000)}"  # per-deploy cache-bust
    return {
        "IMAGE_TAG": image_tag,
        "GIT_COMMIT_SHA": image_tag,
        "NEXT_PUBLIC_APP_URL": alias.app_url(domain=domain),
        "ENV_SUFFIX": alias.env_suffix,
        "ENV_DOMAIN_SUFFIX": alias.domain_suffix,
        # ENV is the alias display label so the telemetry identity contract
        # (core.environments §4.5: deployment.environment = pr-<N>/commit-<sha>/main)
        # is carried into the running stack.
        "ENV": alias.deployment_environment,
        # Vault app-secrets source env (preview has no per-alias Vault path).
        "PREVIEW_SECRET_ENV": _PREVIEW_SECRET_ENV,
        "COMPOSE_PROFILES": "app",
        "TRAEFIK_ENABLE": "true",
        "INTERNAL_DOMAIN": domain,
        "IAC_CONFIG_HASH": config_hash,
        # Ephemeral DB knobs — the compose template builds PREVIEW_DATABASE_URL from
        # these and overrides DATABASE_URL to the local `db` service (never the shared
        # finance_report-postgres). Per-alias name keeps the DSN unambiguous in logs.
        "PREVIEW_DB_USER": "preview",
        "PREVIEW_DB_PASSWORD": "preview",
        "PREVIEW_DB_NAME": "finance_report",
    }


def _find_compose(client, name: str):
    """Return the existing preview compose dict for ``name``, or None."""
    return client.find_compose_by_name(
        name, PREVIEW_PROJECT, env_name=PREVIEW_ENVIRONMENT
    )


def up(
    kind: str,
    value: int | str | None,
    *,
    code: str,
    domain: str,
    client,
    github_id: str | None = None,
    repo_owner: str = "wangzitian0",
    repo_name: str = "infra2",
    branch: str = "main",
    wait: bool = True,
    health_timeout: int = 600,
    health_interval: int = 10,
    repo: str | None = None,
    http_get=None,
    _now=time.time,
    _sleep=time.sleep,
    _monotonic=time.monotonic,
) -> PreviewResult:
    """Stand up (or update) the preview stack for one alias and deploy ``code``.

    Resolves code->sha, computes the alias identity, finds-or-creates this alias's own
    Dokploy compose (GitHub-sourced preview template), pushes the env, triggers a deploy,
    and — when wait=True — polls https://report-<alias>.<domain>/api/health until 200.
    Idempotent: re-running an alias updates and redeploys the same compose in place.
    """
    _validate_domain(domain)

    alias = preview_alias(kind, value)
    sha = resolve_to_sha(code, repo=repo) if repo is not None else resolve_to_sha(code)
    env_vars = _preview_env_vars(alias, sha=sha, domain=domain, _now=_now)
    # Inject the runtime AppRole creds the preview vault-agent logs in with — the same
    # role the source env runs with. Without them vault-agent crash-loops and the app
    # never becomes healthy. Merged before the compose env is pushed below.
    env_vars.update(_source_app_vault_creds(client, _PREVIEW_SECRET_ENV))

    # Find-or-create THIS alias's compose by its deterministic name. The GitHub source
    # fields make Dokploy pull the preview compose template (+ its mounted vault files)
    # from infra2; autoDeploy=False so only this manual lifecycle triggers a deploy.
    gh_id = github_id if github_id is not None else client.get_github_provider_id()
    if not gh_id:
        raise RuntimeError(
            "no GitHub provider configured in Dokploy; add one in Settings -> Git "
            "Providers before deploying a preview (the compose template is pulled from "
            "the infra2 repo, not uploaded raw)."
        )
    source_fields = dict(
        source_type="github",
        githubId=gh_id,
        repository=repo_name,
        owner=repo_owner,
        branch=branch,
        composePath=_PREVIEW_COMPOSE_PATH,
        autoDeploy=False,
    )

    existing = _find_compose(client, alias.compose_name)
    if not existing:
        # Self-provision the preview environment (idempotent). The fixed envs
        # (staging/prod) are pre-provisioned by convention, but preview is DYNAMIC — its
        # composes are created and destroyed per alias — so the lifecycle owns its parent
        # environment too. This makes a fresh box / first-ever preview reproducible with
        # no out-of-band `invoke dokploy_env.env-ensure` step (the missing-env gap that
        # blocked the first live canary). PREVIEW_PROJECT/PREVIEW_ENVIRONMENT are
        # constants, so there is no typo risk in creating-if-absent.
        env_obj, _ = client.ensure_environment(PREVIEW_PROJECT, PREVIEW_ENVIRONMENT)
        environment_id = env_obj.get("environmentId")
        if not environment_id:
            raise RuntimeError(
                f"could not ensure Dokploy environment "
                f"{PREVIEW_PROJECT!r}/{PREVIEW_ENVIRONMENT!r}"
            )
        # Teardown-convergence invariant (#921 / D8): the Dokploy record name AND the
        # appName (the docker project `compose.delete` prunes by) are the SAME alias key,
        # so `down` creates-under and prunes-by one key — never the divergent pair that
        # leaked orphans in infra2#310. Keep these two identical; see
        # libs/tests/test_preview_teardown_convergence.py.
        # Create as github source from the start (Dokploy accepts this and keeps
        # sourceType) so the compose is never momentarily a raw compose with an empty
        # composeFile. The github *binding* (githubId/owner/repository/branch/composePath)
        # still has to be re-applied below — compose.create drops everything but sourceType.
        created = client.create_compose(
            environment_id=environment_id,
            name=alias.compose_name,
            app_name=alias.compose_name,
            source_type="github",
        )
        compose_id = created["composeId"]
    else:
        compose_id = existing["composeId"]

    # Configure source + env on BOTH paths. Dokploy's compose.create persists ONLY the
    # basic fields — it silently drops the github source (githubId/owner/repository/
    # branch/composePath) and the env blob — so a first-ever preview deploy would land
    # source-less and env-less and fail at deploy time ("Github Provider not found" / all
    # compose vars blank → IMAGE_TAG defaults to :latest). The github source + env only
    # stick via these follow-up compose.update / compose.update-env calls, which also
    # re-assert them on a redeploy. MERGE the env so runtime AppRole creds
    # (VAULT_ROLE_ID / VAULT_SECRET_ID / VAULT_ADDR) injected at setup survive a redeploy.
    client.update_compose(compose_id, **source_fields)
    client.update_compose_env(compose_id, env_vars=env_vars)

    client.deploy_compose(compose_id)

    healthy: bool | None = None
    url = alias.app_url(domain=domain)
    if wait:
        healthy = _wait_for_health(
            f"{url}/api/health",
            timeout=health_timeout,
            interval=health_interval,
            http_get=http_get,
            _sleep=_sleep,
            _now=_monotonic,
        )
        if not healthy:
            raise TimeoutError(
                f"preview {alias.alias} did not become healthy at {url}/api/health "
                f"within {health_timeout}s"
            )

    return PreviewResult(
        action="up",
        alias=alias.alias,
        compose_id=compose_id,
        sha=sha,
        url=url,
        healthy=healthy,
    )


def down(
    kind: str,
    value: int | str | None,
    *,
    domain: str,
    client,
) -> PreviewResult:
    """Tear down the preview stack for one alias, destroying its ephemeral DB volume.

    Finds the alias's compose by its deterministic name and deletes it with
    delete_volumes=True so the throwaway `preview_db` named volume is removed too —
    nothing the preview wrote survives teardown. A no-op (compose_id=None) if the
    alias was already gone, so teardown is safely idempotent.
    """
    alias = preview_alias(kind, value)
    existing = _find_compose(client, alias.compose_name)
    url = alias.app_url(domain=domain)
    if not existing:
        return PreviewResult(
            action="down",
            alias=alias.alias,
            compose_id=None,
            sha=None,
            url=url,
            healthy=None,
        )
    compose_id = existing["composeId"]
    client.delete_compose(compose_id, delete_volumes=True)
    return PreviewResult(
        action="down",
        alias=alias.alias,
        compose_id=compose_id,
        sha=None,
        url=url,
        healthy=None,
    )


def _http_get(url: str, timeout: float) -> tuple[int, str]:
    """Minimal GET returning (status, body-head). 0 status on a connection error.

    Mirrors libs.dokploy_route_canary._http_get so preview readiness uses the same
    stdlib-only probe; an injected getter replaces it in tests (no real HTTP).
    """
    request = Request(url, headers={"User-Agent": "infra2-preview-lifecycle/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read(256).decode("utf-8", errors="replace")
    except HTTPError as exc:
        return exc.code, exc.read(256).decode("utf-8", errors="replace")
    except (URLError, TimeoutError) as exc:
        return 0, str(getattr(exc, "reason", exc))


def _wait_for_health(
    health_url: str,
    *,
    timeout: int,
    interval: int,
    http_get=None,
    _sleep=time.sleep,
    _now=time.monotonic,
) -> bool:
    """Poll ``health_url`` until it returns HTTP 200, or the deadline passes.

    Returns True on the first 200, False if the window elapses first. Side-effect free
    apart from the injected getter, so tests drive it with a fake clock + fake getter.
    """
    getter = http_get or _http_get
    deadline = _now() + max(0, timeout)
    while True:
        status, _ = getter(health_url, 10)
        if status == 200:
            return True
        if _now() >= deadline:
            return False
        _sleep(max(1, interval))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="multi-alias preview lifecycle")
    sub = parser.add_subparsers(dest="action", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--kind", required=True, choices=["main", "pr", "commit"])
    common.add_argument(
        "--value", default=None, help="PR number or commit sha (omit for main)"
    )
    common.add_argument(
        "--domain", required=True, help="base domain, e.g. zitian.party"
    )

    up_p = sub.add_parser(
        "up", parents=[common], help="create/update + deploy an alias"
    )
    up_p.add_argument(
        "--code", required=True, help="main | release/x.y | vX.Y.Z | <sha>"
    )
    up_p.add_argument("--repo", default=None, help="git remote to resolve code against")
    up_p.add_argument("--no-wait", action="store_true", help="do not health-check")
    up_p.add_argument("--timeout", type=int, default=600, help="health-check seconds")

    sub.add_parser(
        "down", parents=[common], help="tear down an alias + its ephemeral DB"
    )

    args = parser.parse_args(argv)

    # Validate the domain BEFORE it is used to build the Dokploy host, so a malformed
    # domain is rejected up front for both `up` and `down` (the client host is derived
    # from it; up()/down() also re-validate, but the client must not be built from a bad
    # domain in the first place).
    try:
        _validate_domain(args.domain)
    except ValueError as exc:
        print(f"preview {args.action} failed: {exc}", file=sys.stderr)
        return 2

    # Imported lazily so importing the module (and its unit tests) needs no Dokploy creds.
    from libs.dokploy import get_dokploy

    client = get_dokploy(host=f"cloud.{args.domain}")
    try:
        if args.action == "up":
            result = up(
                args.kind,
                args.value,
                code=args.code,
                domain=args.domain,
                client=client,
                wait=not args.no_wait,
                health_timeout=args.timeout,
                repo=args.repo,
            )
        else:
            result = down(args.kind, args.value, domain=args.domain, client=client)
    except (ValueError, RuntimeError, TimeoutError) as exc:
        print(f"preview {args.action} failed: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "action": result.action,
                "alias": result.alias,
                "compose_id": result.compose_id,
                "sha": result.sha,
                "url": result.url,
                "healthy": result.healthy,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
