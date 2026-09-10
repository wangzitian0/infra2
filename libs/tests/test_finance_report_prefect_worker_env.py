"""The finance_report Prefect worker runs the backend image and needs its configuration.

Until #639 the vault-agent template rendered PREFECT_API_URL (with the platform default)
into /secrets/.env for every container of the stack; #639 moved it into the compose file
for `backend` only. The worker then let Prefect start an ephemeral API server, which timed
out, and the container crash-looped at ~1.5 cores per environment (2026-09-08, 71 restarts
on production). Every service of the stack that talks to Prefect names the server here.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "finance_report/finance_report/10.app/compose.yaml"
TEMPLATE = ROOT / "finance_report/finance_report/10.app/secrets.ctmpl"
PREFECT_SERVER = "http://platform-prefect-server${ENV_SUFFIX}:4200/api"


def _environment(service: dict) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        return dict(item.split("=", 1) for item in env)
    return {str(k): str(v) for k, v in env.items()}


def test_backend_and_worker_both_name_the_prefect_server() -> None:
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    for name in ("backend", "prefect-worker"):
        assert _environment(services[name]).get("PREFECT_API_URL") == PREFECT_SERVER, (
            name
        )


def test_the_template_no_longer_carries_the_value_so_compose_is_the_only_source() -> (
    None
):
    assert "PREFECT_API_URL" not in TEMPLATE.read_text(encoding="utf-8")


#: Configuration BOTH processes that run the backend image need. The worker parses
#: statements: same object store, same models, same environment identity.
SHARED_RUNTIME_KEYS = (
    "ENVIRONMENT",
    "S3_ENDPOINT",
    "S3_BUCKET",
    "S3_PUBLIC_BUCKET",
    "S3_PRESIGN_EXPIRY_SECONDS",
    "PRIMARY_MODEL",
    "FALLBACK_MODELS",
    "PREFECT_API_URL",
)
#: NOT shared. The OTLP endpoint turns export on, and the app refuses to start when
#: export is on without a deployment.environment tag in OTEL_RESOURCE_ATTRIBUTES, which
#: the deployer issues per component. See the comment on the anchor.
BACKEND_ONLY_KEYS = ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_SERVICE_NAME", "CORS_ORIGINS")


def test_the_worker_gets_the_same_runtime_configuration_as_the_backend() -> None:
    """2026-09-10: the staging worker had no S3_BUCKET, fell back to the image default
    `statements` — production's bucket — and every statement it tried to parse died with
    `StorageError: Failed to access bucket statements` once #677 scoped the staging
    credential to its own bucket. Production only worked because the default happened to
    be the right name there. The release that was blocked by it is the one carrying two
    critical Next.js RCEs.

    This is the second instance of the same shape: #639 moved PREFECT_API_URL into the
    compose file "but only for backend". Hence one anchor, and this test.
    """
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    backend = _environment(services["backend"])
    worker = _environment(services["prefect-worker"])

    for key in SHARED_RUNTIME_KEYS:
        assert key in backend, f"backend lost {key}"
        assert worker.get(key) == backend[key], (
            f"prefect-worker's {key} differs from backend's — both run the same image "
            "and parse the same statements"
        )


def test_the_worker_does_not_get_telemetry_it_cannot_satisfy() -> None:
    """Handing the worker OTEL_EXPORTER_OTLP_ENDPOINT alone crash-looped staging within a
    minute of the deploy: export on, no deployment.environment tag, Settings() refuses to
    construct. Backend-only values stay on the backend."""
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    backend = _environment(services["backend"])
    worker = _environment(services["prefect-worker"])

    for key in BACKEND_ONLY_KEYS:
        assert key in backend, f"backend lost {key}"
        assert key not in worker, (
            f"{key} on the worker turns on behaviour it has no configuration for"
        )
