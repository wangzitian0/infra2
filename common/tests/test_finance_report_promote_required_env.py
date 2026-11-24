"""finance_report f23ce517 stops in staging and production on an empty declared name
(#1466): `boot.py` runs `assert_environment(require_injected=True)`, where an empty value
is absent and `empty_ok` exempts a name only while it is unset. Names come from
`10.app/env.manifest.json`, the projection of the backend and frontend manifests at the
pinned release (`tools/app_manifest_sync.py`). The backend container boots on the backend
fields; the frontend container gets the `Frontend` group fields. No test reads the network.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from infra2_sdk.runtime.config_schema import EnvironmentManifest, validate_environment

from common.deploy import promote
from common.deploy.env_config import app_compose_env_config
from common.tests.compose_env import compiled_vault_template, container_env

APP = Path(__file__).resolve().parents[2] / "finance_report/finance_report/10.app"
MANIFEST = APP / "env.manifest.json"
COPY = EnvironmentManifest.from_dict(json.loads(MANIFEST.read_text()))
#: The fields of the finance_report frontend manifest (apps/frontend/required-env.manifest.json).
FRONTEND = EnvironmentManifest(
    source=COPY.source, fields=tuple(f for f in COPY.fields if f.group == "Frontend")
)
BACKEND = EnvironmentManifest(
    source=COPY.source, fields=tuple(f for f in COPY.fields if f.group != "Frontend")
)
FIELDS = BACKEND.fields
VAULT = re.findall(r"^(\w+)=", compiled_vault_template(APP), re.M)
SERVICE = "finance_report/app"


def container_environ(env: str, container: str) -> dict[str, str]:
    """Container environment of a promote: compose, deploy builder, Vault lines."""
    cfg = app_compose_env_config(SERVICE, env)
    args = (SERVICE, env, "zitian.party", env.replace("prod", "production"), cfg)
    variables, _ = promote._build_deploy_env_vars(*args, "7913891", "a" * 40, "x")
    environ = container_env(APP / "compose.yaml", container, variables)
    return {**environ, **dict.fromkeys(VAULT, "<vault>")}


def backend_environ(env: str) -> dict[str, str]:
    return container_environ(env, "backend")


def refused_names(
    environ: dict[str, str], manifest: EnvironmentManifest = BACKEND
) -> list[str]:
    """Names that the container refuses to start without; it defaults the unset ones."""
    defaults = {
        f.env: "<default>" for f in manifest.fields if f.empty_ok or f.source == "code"
    }
    result = validate_environment(
        manifest, {**defaults, **environ}, require_injected=True
    )
    return list(result.missing)


def test_the_frontend_group_is_the_frontend_manifest() -> None:
    assert [f.env for f in FRONTEND.fields] == [
        "NEXT_PUBLIC_API_URL",
        "NEXT_PUBLIC_DEPLOYMENT_ENVIRONMENT",
        "NEXT_PUBLIC_GIT_SHA",
        "NEXT_PUBLIC_OTEL_EXPORTER_OTLP_ENDPOINT",
        "OPENPANEL_SCRIPT_URL",
    ]


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_the_promote_environment_has_every_declared_name(env: str) -> None:
    assert refused_names(backend_environ(env)) == []


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_the_frontend_promote_environment_has_every_frontend_name(env: str) -> None:
    environ = container_environ(env, "frontend")
    assert refused_names(environ, FRONTEND) == []
    # The frontend container refuses a start without its injected SHA.
    del environ["NEXT_PUBLIC_GIT_SHA"]
    assert refused_names(environ, FRONTEND) == ["NEXT_PUBLIC_GIT_SHA"]


def test_the_public_bucket_is_the_bucket_in_use() -> None:
    """finance_report reads `s3_public_bucket or bucket`; the default keeps that bucket."""
    for env, bucket in (("prod", "statements"), ("staging", "finance-report-staging")):
        environ = backend_environ(env)
        assert environ["S3_PUBLIC_BUCKET"] == environ["S3_BUCKET"] == bucket


def test_an_empty_ok_name_may_be_unset_but_not_empty() -> None:
    environ = {f.env: "value" for f in FIELDS if f.injected}
    assert refused_names(environ) == []
    assert refused_names({**environ, "PREFECT_API_URL": ""}) == ["PREFECT_API_URL"]
    assert refused_names({**environ, "S3_PUBLIC_BUCKET": ""}) == ["S3_PUBLIC_BUCKET"]
    del environ["S3_PUBLIC_BUCKET"]
    assert refused_names(environ) == []
