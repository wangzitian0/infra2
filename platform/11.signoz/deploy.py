"""SigNoz deployment - observability platform"""

import os
import shlex
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

from libs.deploy.deployer import Deployer, make_tasks
from libs.console import success, info, run_with_status, error, warning
from libs.common import (
    OTEL_INGEST_SUBDOMAIN,
    otel_ingest_endpoint,
    service_domain,
)
from libs.service_facets import PublicRouteFacet, BackupFacet, ProbeFacet, SignalFacet

shared_tasks = sys.modules.get("platform.11.signoz.shared")


class SigNozDeployer(Deployer):
    service = "signoz"
    compose_path = "platform/11.signoz/compose.yaml"
    data_path = "/data/platform/signoz"

    # Backup facts (#542): the backup inventory derives from these
    # (formerly the ops.backup-inventory YAML, deleted).
    backups = (
        BackupFacet(
            method="signoz_filesystem_archive",
            restore_command="restore SigNoz data and collector config while service is stopped.",
        ),
    )
    # Observability — single prod instance; all envs ship here. No staging copy.
    prod_only = True

    # Routing is Dokploy-managed (Infra-014 follow-up): the base deployer flow
    # registers the SigNoz Web UI domain (signoz.<domain> → signoz:8080) from these
    # attributes, and composing() below registers the SECOND domain (otel.<domain> →
    # otel-collector:4318) for the public browser-OTLP ingest. No hand-written Traefik
    # labels in compose.yaml — see docs/ssot/platform.domain.md and
    # libs/tests/test_domain_routing_policy.py.
    subdomain = "signoz"

    # Public route probed from inside (#543, #209 reversed). prod_only: the
    # renderer emits this for production only (staging host never exists).
    public_routes = (
        PublicRouteFacet(
            name="signoz-public-route",
            path="/api/v1/health",
            expected="200,301,302,401",
        ),
    )
    service_port = 8080  # SigNoz unified container port
    service_name = "signoz"

    # Public browser-OTLP ingest domain: otel.<domain> → otel-collector:4318.
    # The subdomain + the FE endpoint live in libs.common (#368, ONE source).
    otel_ingest_subdomain = OTEL_INGEST_SUBDOMAIN
    otel_ingest_service_name = "otel-collector"

    # Infra probes (#541): rendered into INFRA_PROBE_SPECS by platform/alerting.
    # SigNoz is prod_only (single shared instance; every env ships its data to
    # prod — `prod_only = True` above), so targets carry NO ${ENV_SUFFIX}: probe
    # the prod instance from ALL envs. Adding a -staging suffix would target a
    # host that never exists -> a permanent false-positive alert. The renderer's
    # tests enforce this from the registry's prod_only fact.
    probes = (
        ProbeFacet(
            name="signoz-internal-http",
            kind="http",
            target="http://platform-signoz:8080/api/v1/health",
            expected="200",
        ),
        # otel 畅通: the OTLP ingest pipeline itself (collector health_check
        # ext :13133), distinct from the signoz query svc above.
        ProbeFacet(
            name="otel-collector-http",
            kind="http",
            target="http://platform-signoz-otel-collector:13133",
            expected="200",
        ),
        # SigNoz round-trip: write a synthetic OTLP log through the collector,
        # then query ClickHouse for the same nonce. This proves ingest +
        # storage, not just process readiness. depends_on signoz-internal-http:
        # if SigNoz itself is down the round-trip also fails — cascade-suppress
        # the round-trip symptom, page the signoz root only.
        ProbeFacet(
            name="signoz-roundtrip",
            kind="command",
            target="python /app/tools/observability_roundtrip_probe.py signoz",
            expected="roundtrip-ok",
            timeout_seconds=45,
            depends_on="signoz-internal-http",
        ),
    )
    # Signal classification (#425 T5 / #543): every probe above is a
    # minute-tier alert debounced by the probe runner's shared loop —
    # DEFAULT_FAILURE_THRESHOLD=3 / DEFAULT_RENOTIFY_SECONDS=1800
    # (tools/infra_probe_runner.py). watchdog-signals entries derive from this
    # (libs/watchdog_signal_entries.py); the values here must state what the
    # runner actually does, not an aspiration.
    signals = (
        SignalFacet(
            tier="minute",
            type="alert",
            consecutive_failures=3,
            renotify_window_sec=1800,
        ),
    )

    otel_ingest_port = 4318

    # SigNoz specific secret
    secret_key = "jwt_secret"

    @classmethod
    def _render_collector_config(cls, e) -> str:
        """Render otel-collector-config.yaml from its template (#368/#372).

        Substitutes ``${ENV_SUFFIX}`` (clickhouse DSN target) and the
        ``${OTEL_CORS_ALLOWED_ORIGINS}`` block, which is DERIVED from the FE origins
        (libs.deploy_env_config.cors_allowed_origins) so the CORS allow-list can
        never drift from the actual app domains it must mirror.
        """
        from libs.deploy_env_config import cors_allowed_origins

        template_path = Path(__file__).with_name("otel-collector-config.yaml")
        content = template_path.read_text()
        content = content.replace("${ENV_SUFFIX}", e.get("ENV_SUFFIX") or "")

        domain = e.get("INTERNAL_DOMAIN") or "localhost"
        # Render as a YAML *flow* sequence on the `allowed_origins:` line — e.g.
        # ["https://report.zitian.party", ...]. Inline so the raw, unrendered template
        # is itself valid YAML (the placeholder is just a scalar value until replaced),
        # which keeps editors / YAML lints happy. Flow lists are valid YAML the
        # collector parses identically to a block list.
        origins = cors_allowed_origins(domain=domain)
        flow_seq = "[" + ", ".join(f'"{origin}"' for origin in origins) + "]"
        return content.replace("${OTEL_CORS_ALLOWED_ORIGINS}", flow_seq)

    @classmethod
    def _deliver_collector_config(cls, c, e) -> bool:
        """Render + upload otel-collector-config.yaml to the host (idempotent).

        The collector mounts this file read-only, so it must be on the host BEFORE
        the container is (re)created. Called from BOTH pre_compose (first setup) AND
        composing — so `sync`/redeploys ship config changes too (#372): `sync` skips
        pre_compose's side effects but always runs composing. The rendered content
        derives its CORS allow-list from the FE origins (#368).
        """
        host = e.get("VPS_HOST")
        if not host:
            error("Missing VPS_HOST")
            return False
        data_path = cls.data_path_for_env(e)
        # Persistent disk-queue dir for the collector's exporter sending_queue (#369).
        # Must exist + be writable by the non-root collector (uid 10001) BEFORE the
        # container starts, on EVERY deploy path — `sync` skips pre_compose but always
        # runs composing → this helper. Without it Docker auto-creates the bind dir
        # root-owned and the collector crash-loops on the file_storage extension.
        # shlex.quote the path/host so a DATA_PATH/VPS_HOST with spaces or shell
        # metacharacters can't break or reinterpret the remote command.
        queue_dir = shlex.quote(f"{data_path}/otel-queue")
        remote_cmd = (
            f"mkdir -p {queue_dir} && chown -R 10001:0 {queue_dir} "
            f"&& chmod 770 {queue_dir}"
        )
        if not run_with_status(
            c,
            f"ssh {shlex.quote(f'root@{host}')} {shlex.quote(remote_cmd)}",
            "Prepare OTel collector disk-queue directory",
        ).ok:
            return False
        template_path = Path(__file__).with_name("otel-collector-config.yaml")
        if not template_path.exists():
            error("Missing otel-collector config template", str(template_path))
            return False
        config_path = f"{data_path}/otel-collector-config.yaml"
        tmp_path = None
        try:
            config_content = cls._render_collector_config(e)
            with NamedTemporaryFile("w", delete=False, encoding="utf-8") as tmp:
                tmp.write(config_content)
                tmp_path = tmp.name
            if not run_with_status(
                c,
                f"scp {tmp_path} root@{host}:{config_path}",
                "Upload otel-collector config",
            ).ok:
                return False
            if not run_with_status(
                c,
                f"ssh root@{host} 'chmod 644 {config_path}'",
                "Set otel-collector config permissions",
            ).ok:
                return False
        except OSError as exc:
            error("Failed to prepare otel-collector config", str(exc))
            return False
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
        return True

    @classmethod
    def pre_compose(cls, c):
        """Prepare directories and secrets for SigNoz."""
        if not cls._prepare_dirs(c):
            return None

        e = cls.env()
        data_path = cls.data_path_for_env(e)
        host = e.get("VPS_HOST")
        if not host:
            error("Missing VPS_HOST")
            return None
        secrets_backend = cls.secrets_backend()

        # Create data directory for query-service SQLite
        result = run_with_status(
            c, f"ssh root@{host} 'mkdir -p {data_path}/data'", "Create data directory"
        )
        if not result.ok:
            return None

        # Set permissions (SigNoz runs as root in container, but let's be explicit)
        result = run_with_status(
            c, f"ssh root@{host} 'chmod -R 755 {data_path}'", "Set permissions"
        )
        if not result.ok:
            return None

        # #372: config delivery is shared with composing() so `sync`/redeploys
        # (which skip pre_compose's side effects) also ship collector-config changes.
        if not cls._deliver_collector_config(c, e):
            return None

        if not cls.ensure_runtime_secrets(c):
            return None
        jwt_secret = secrets_backend.get(cls.secret_key)

        success("pre_compose complete")
        domain_suffix = e.get("ENV_DOMAIN_SUFFIX", "")
        info(
            f"Frontend will be available at: https://signoz{domain_suffix}.{e.get('INTERNAL_DOMAIN', 'localhost')}"
        )
        info("OTLP endpoints: 4317 (gRPC), 4318 (HTTP)")
        # #368: same single source as the FE compose endpoint — no second literal.
        ingest_endpoint = otel_ingest_endpoint(e) or "(INTERNAL_DOMAIN unset)"
        info(f"Public browser-OTLP ingest (CORS-gated, no bearer): {ingest_endpoint}")

        result = cls.compose_env_base(e)
        result["SIGNOZ_JWT_SECRET"] = jwt_secret
        return result

    @classmethod
    def composing(cls, c, env_vars):
        """Deploy via the base flow, then register the SECOND Dokploy-managed domain.

        The base composing() registers ONE domain (signoz.<domain> → signoz:8080) from
        cls.subdomain/service_port/service_name. The public browser-OTLP ingest needs a
        second domain on the same compose (otel.<domain> → otel-collector:4318), so we
        add an extra ensure_domains() call here. This keeps routing Dokploy-managed (no
        hand-written Traefik labels) and is idempotent — ensure_domains skips domains
        that already exist.
        """
        e = cls.env()
        # #372: ship the collector config to the host BEFORE the (re)deploy so
        # `sync`/redeploys actually apply config changes (the container mounts it
        # read-only; sync skips pre_compose, so deliver it here too).
        if not cls._deliver_collector_config(c, e):
            raise RuntimeError("Failed to deliver otel-collector config to host")
        compose_id = super().composing(c, env_vars)

        otel_host = service_domain(cls.otel_ingest_subdomain, e)
        if not otel_host:
            warning("OTLP ingest domain skipped: INTERNAL_DOMAIN missing")
            return compose_id

        from libs.dokploy import get_dokploy

        domain = e.get("INTERNAL_DOMAIN")
        client = get_dokploy(host=f"cloud.{domain}" if domain else None)

        info(f"Ensuring OTLP ingest domain: {otel_host}")
        result = client.ensure_domains(
            compose_id=compose_id,
            desired_domains=[
                {"host": otel_host, "port": cls.otel_ingest_port, "https": True}
            ],
            service_name=cls.otel_ingest_service_name,
        )
        if result["created"] > 0:
            success(f"OTLP ingest domain configured: https://{otel_host}")
            info("Redeploying to apply domain labels...")
            cls._deploy_compose_with_record_check(client, compose_id)
            success("OTLP ingest domain labels updated")
        elif result["skipped"] > 0:
            info(f"OTLP ingest domain already configured: {otel_host}")
        for conflict in result["conflicts"]:
            warning(
                f"Domain conflict: {conflict['host']} exists with port "
                f"{conflict['existing_port']}, need {conflict['desired_port']}"
            )
        # ensure_domains() reports create failures in `errors` rather than raising;
        # surface them loudly (and fail) so otel.<domain> is never left silently
        # unconfigured — otherwise FE telemetry would drop with no deploy-time signal.
        errors = result.get("errors") or []
        for err in errors:
            error(f"OTLP ingest domain error for {otel_host}: {err}")
        if errors:
            raise RuntimeError(
                f"Failed to configure OTLP ingest domain {otel_host}: "
                f"{len(errors)} error(s) — see log above."
            )

        return compose_id


if shared_tasks:
    _tasks = make_tasks(SigNozDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
