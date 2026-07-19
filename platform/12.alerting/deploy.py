"""Alerting bridge deployment."""

import sys

from libs.deploy.deployer import Deployer, make_tasks
from libs.env import get_secrets
from libs.console import error, info, success
from libs.service_facets import ProbeFacet

shared_tasks = sys.modules.get("platform.12.alerting.shared")


class AlertingDeployer(Deployer):
    service = "alerting"
    compose_path = "platform/12.alerting/compose.yaml"
    data_path = "/data/platform/alerting"

    subdomain = None
    service_port = 8080
    service_name = "feishu-alert-bridge"
    runtime_only_config_keys = frozenset(
        {"INFRA_PROBE_HEARTBEAT_URL", "INFRA_PROBE_HEARTBEAT_TOKEN"}
    )

    # Infra probes (#541). Two groups:
    #
    # 1) This service's OWN probes (no explicit service_id — it derives).
    # 2) Probes for OUT-OF-REGISTRY components, declared here with an explicit
    #    `service_id`: the bootstrap plane (dokploy/vault/1password/iac-runner)
    #    and the host itself have no Deployer in the registry's layer scan
    #    (bootstrap is deployed by bootstrap tooling; `infra/host` is not a
    #    service at all), yet alerting owns the probe-runner that watches them.
    #    Declaring them on the runner's own Deployer keeps ONE declaration
    #    point + ONE derivation (service_attrs) with zero extra registries.
    #
    # NOTE: the 6h real-send `alert-delivery-canary` was retired (#425 T3): it
    # posted a synthetic *alert* every 6h purely to prove the bridge→Feishu
    # path — a periodic alert (the anti-pattern #425 forbids). The path is now
    # covered without channel noise by `lark-delivery-http` (config valid +
    # Feishu reachable, no real post), the out-of-band watchdog's independent
    # bridge /health check, the daily reports' own Feishu delivery, and real
    # alerts when they fire.
    probes = (
        ProbeFacet(
            name="alert-bridge-http",
            kind="http",
            target="http://platform-alerting${ENV_SUFFIX}:8080/health",
            expected="200",
        ),
        # lark 畅通: bridge config valid AND Feishu host reachable (TCP 443),
        # checked without posting to the real channel (see app.py /health/feishu).
        ProbeFacet(
            name="lark-delivery-http",
            kind="http",
            target="http://platform-alerting${ENV_SUFFIX}:8080/health/feishu",
            expected="200",
        ),
        # --- bootstrap plane (out-of-registry service_ids) ---
        ProbeFacet(
            name="dokploy-internal-http",
            kind="http",
            target="http://dokploy:3000",
            expected="200,302",
            service_id="bootstrap/dokploy",
        ),
        ProbeFacet(
            name="vault-internal-http",
            kind="http",
            target="http://vault:8200/v1/sys/health",
            expected="200,429,472,473",
            service_id="bootstrap/vault",
        ),
        ProbeFacet(
            name="op-connect-http",
            kind="http",
            target="http://op-connect-api:8080/health",
            expected="200",
            service_id="bootstrap/1password",
        ),
        ProbeFacet(
            name="iac-runner-http",
            kind="http",
            target="http://iac-runner:8080/health",
            expected="200",
            severity="warning",
            service_id="bootstrap/iac-runner",
        ),
        # --- host resource backstop (out-of-band from SigNoz, so it still
        # fires when the host is starved — the dockerd-busy-loop class of
        # incident). `expected` is the % ceiling; reads host-global /proc and
        # the bind-mounted host root. Runs on the PRODUCTION runner only (the
        # host is shared) — see infra_probe_runner._host_specs_for_env.
        ProbeFacet(
            name="host-cpu",
            kind="resource",
            target="cpu",
            expected="80",
            severity="warning",
            service_id="infra/host",
        ),
        ProbeFacet(
            name="host-mem",
            kind="resource",
            target="mem",
            expected="80",
            severity="warning",
            service_id="infra/host",
        ),
        ProbeFacet(
            name="host-disk",
            kind="resource",
            target="disk:/hostfs",
            expected="80",
            service_id="infra/host",
        ),
    )

    @classmethod
    def _probe_specs_env_value(cls, e) -> str:
        """The registry-rendered INFRA_PROBE_SPECS as ONE dotenv-safe env value.

        Derivation chain (#541): every service's deploy.py ProbeFacets ->
        service_attrs() -> render_probe_spec_text(). ${ENV_SUFFIX} placeholders
        are resolved HERE from the deploy env (this used to be compose
        interpolation's job when the literal lived in compose.yaml), then the
        multi-line text is encoded for the Dokploy env transport (double
        quotes + \\n escapes, which compose's dotenv expands back).
        """
        from libs.probe_specs import (
            encode_specs_env_value,
            render_probe_spec_text,
            resolve_env_suffix,
        )

        text = resolve_env_suffix(render_probe_spec_text(), e.get("ENV_SUFFIX") or "")
        return encode_specs_env_value(text)

    @classmethod
    def compose_env_base(cls, env=None):
        """Rendered probe specs + probe heartbeat runtime env for Dokploy.

        MUST be compose_env_base, not pre_compose: the iac-runner's sync path
        builds the Dokploy env straight from compose_env_base, skipping
        pre_compose entirely (the v1.1.24 lesson — see
        truealpha/truealpha/01.postgres/deploy.py).
        """
        e = env or cls.env()
        result = super().compose_env_base(e)
        result["INFRA_PROBE_SPECS"] = cls._probe_specs_env_value(e)
        op_secrets = get_secrets(
            project=cls.project_name(e),
            service=cls.service,
            env=e.get("ENV", "production"),
            credential_type="root_vars",
        )
        for key in ("INFRA_PROBE_HEARTBEAT_URL", "INFRA_PROBE_HEARTBEAT_TOKEN"):
            value = op_secrets.get(key)
            if value:
                result[key] = value
        return result

    @classmethod
    def source_config_env_base(cls, env=None):
        """Build release identity without reading runtime heartbeat secrets.

        The rendered probe specs ARE part of the source identity: they derive
        purely from git (deploy.py facets), so a probe change must flip the
        source hash exactly as editing the old compose literal did.
        """
        e = env or cls.env()
        result = super().compose_env_base(e)
        result["INFRA_PROBE_SPECS"] = cls._probe_specs_env_value(e)
        return result

    @classmethod
    def pre_compose(cls, c):
        """Sync 1Password alerting root vars into Vault runtime secrets."""
        if not cls._prepare_dirs(c):
            return None

        if not cls._sync_1password_to_vault():
            return None

        e = cls.env()
        result = cls.compose_env_base(e)
        result["VAULT_ADDR"] = e.get(
            "VAULT_ADDR", f"https://vault.{e.get('INTERNAL_DOMAIN', 'localhost')}"
        )
        success("pre_compose complete - Vault runtime secrets synced from 1Password")
        return result

    @classmethod
    def sync(cls, c, force=False):
        if not cls._sync_1password_to_vault():
            return {"action": "failed", "details": "1Password to Vault sync failed"}
        return super().sync(c, force=force)

    @classmethod
    def verify_runtime_applied(cls, c, env_vars):
        """Confirm the running probe runner actually carries every probe the
        registry rendered into the deployed INFRA_PROBE_SPECS env value. Closes
        the gap where a deploy records the intended hash but Dokploy never
        recreated the container, leaving stale probe specs while the catalog
        claims the probes are 'Live'.

        Post-cutover (#541) this ALSO proves the env-value transport end to
        end: if Dokploy/compose ever failed to expand the encoded value back
        into per-line specs, the running container would miss every probe name
        and the deploy would fail loudly here instead of going silently blind.
        """
        import time

        from libs.console import warning
        from libs.probe_specs import missing_probe_names, normalize_specs_text

        e = cls.env()
        host = e.get("VPS_HOST")
        if not host:
            warning("verify_runtime_applied: VPS_HOST unset; skipping runtime check")
            return None

        # The source of truth is the value this very deploy shipped (rendered
        # from the ProbeFacet registry by compose_env_base) — the compose file
        # only carries the ${INFRA_PROBE_SPECS} reference now.
        source_specs = normalize_specs_text(env_vars.get("INFRA_PROBE_SPECS", ""))
        if not source_specs:
            return None  # nothing declared to verify

        ssh_user = e.get("VPS_SSH_USER") or "root"
        suffix = e.get("ENV_SUFFIX", "")
        container = f"platform-alerting-probes{suffix}"

        # Poll against a deadline: Dokploy recreates the container asynchronously,
        # so the new specs can take up to ~a minute to appear (longer under load).
        # A fixed 3 tries (~10s) raced the recreate and failed deploys whenever it
        # lagged; poll until the container carries the new specs or the window
        # elapses.
        deadline = time.monotonic() + 90
        last_err = ""
        while True:
            result = c.run(
                f"ssh {ssh_user}@{host} "
                f"'docker exec {container} printenv INFRA_PROBE_SPECS'",
                warn=True,
                hide=True,
            )
            if not result.failed:
                missing = missing_probe_names(source_specs, result.stdout or "")
                if not missing:
                    success(f"Runtime verified: {container} carries all source probes")
                    return None
                last_err = (
                    f"{container} is missing {len(missing)} probe(s) from the deployed "
                    f"INFRA_PROBE_SPECS ({', '.join(missing)}) — container did not pick "
                    "up the new config (likely not recreated)"
                )
            else:
                last_err = (
                    f"could not read INFRA_PROBE_SPECS from {container}: "
                    f"{(result.stderr or 'docker exec failed').strip()}"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return last_err
            time.sleep(min(5.0, remaining))

    @classmethod
    def _sync_1password_to_vault(cls) -> bool:
        e = cls.env()
        env_name = e.get("ENV", "production")
        project = cls.project_name(e)

        op_secrets = get_secrets(
            project=project,
            service=cls.service,
            env=env_name,
            credential_type="root_vars",
        )
        vault_secrets = get_secrets(
            project=project,
            service=cls.service,
            env=env_name,
            credential_type="app_vars",
        )

        mode = op_secrets.get("ALERT_DELIVERY_MODE") or "feishu_webhook"
        required_by_mode = {
            "feishu_webhook": ["FEISHU_WEBHOOK_URL"],
            "feishu_app": ["FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_CHAT_ID"],
        }
        if mode not in required_by_mode:
            error(f"Unsupported ALERT_DELIVERY_MODE in 1Password: {mode}")
            return False

        keys = [
            "ALERT_DELIVERY_MODE",
            "FEISHU_WEBHOOK_URL",
            "FEISHU_APP_ID",
            "FEISHU_APP_SECRET",
            "FEISHU_CHAT_ID",
            "FEISHU_API_BASE",
            "BRIDGE_BASIC_AUTH_USERNAME",
            "BRIDGE_BASIC_AUTH_PASSWORD",
            "INFRA_PROBE_HEARTBEAT_URL",
            "INFRA_PROBE_HEARTBEAT_TOKEN",
        ]
        values = {key: op_secrets.get(key) for key in keys}
        values["ALERT_DELIVERY_MODE"] = mode

        missing = [key for key in required_by_mode[mode] if not values.get(key)]
        if missing:
            error(
                "Missing alerting secrets in 1Password root_vars",
                f"item={project}/{env_name}/{cls.service} keys={', '.join(missing)}",
            )
            return False

        for key, value in values.items():
            if value is None:
                continue
            if not vault_secrets.set(key, value):
                error(f"Failed to sync {key} into Vault runtime secret")
                return False

        info(f"Alerting delivery mode: {mode}")
        success("Synced alerting runtime secrets from 1Password to Vault")
        return True


if shared_tasks:
    _tasks = make_tasks(AlertingDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
