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
#: Backend-only, and must stay that way. The OTLP endpoint is the sharp case — it turns
#: export on, and the app refuses to start when export is on without a
#: deployment.environment tag in OTEL_RESOURCE_ATTRIBUTES, which the deployer issues per
#: component (see the comment on the anchor). CORS_ORIGINS and OTEL_SERVICE_NAME are the
#: quiet case: they describe the HTTP service, which the worker is not.
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


def test_backend_only_configuration_stays_off_the_worker() -> None:
    """The worker takes the shared anchor and nothing else.

    Motivating case: handing it OTEL_EXPORTER_OTLP_ENDPOINT alone crash-looped staging
    within a minute of the deploy — export on, no deployment.environment tag, Settings()
    refuses to construct. The rest of the list is the same invariant without the drama:
    CORS_ORIGINS and OTEL_SERVICE_NAME describe the HTTP service, and a worker claiming
    them would be describing something it is not.
    """
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    backend = _environment(services["backend"])
    worker = _environment(services["prefect-worker"])

    for key in BACKEND_ONLY_KEYS:
        assert key in backend, f"backend lost {key}"
        assert key not in worker, (
            f"{key} is backend-only and must not appear on the worker"
        )
