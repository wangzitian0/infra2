"""finance_report f23ce517 stops in staging and production on an empty declared name
(#1466): `boot.py` runs `assert_environment(require_injected=True)`, where an empty value
is absent and `empty_ok` exempts a name only while it is unset. Names come from
`10.app/env.manifest.json`, infra2's copy of the manifest (equal to f23ce517; the deploy
preflight `app_contract.unmet` refuses a release that declares more). Refresh: add the
field that the preflight names. No test reads the network.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from infra2_sdk.runtime.config_schema import EnvironmentManifest, assert_environment

from common.deploy import promote
from common.deploy.env_config import app_compose_env_config
from common.tests.compose_env import container_env

APP = Path(__file__).resolve().parents[2] / "finance_report/finance_report/10.app"
MANIFEST = APP / "env.manifest.json"
FIELDS = EnvironmentManifest.from_dict(json.loads(MANIFEST.read_text())).fields
VAULT = re.findall(r"^(\w+)=", (APP / "secrets.ctmpl").read_text(), re.M)
SERVICE = "finance_report/app"


def backend_environ(env: str) -> dict[str, str]:
    """Backend container environment of a promote: compose, deploy builder, Vault lines."""
    cfg = app_compose_env_config(SERVICE, env)
    args = (SERVICE, env, "zitian.party", env.replace("prod", "production"), cfg)
    variables, _ = promote._build_deploy_env_vars(*args, "7913891", "a" * 40, "x")
    environ = container_env(APP / "compose.yaml", "backend", variables)
    return {**environ, **dict.fromkeys(VAULT, "<vault>")}


def refused_names(environ: dict[str, str]) -> list[str]:
    """Names that finance_report refuses to start without; it defaults the unset ones."""
    defaults = {f.env: "<default>" for f in FIELDS if f.empty_ok or f.source == "code"}
    environ = {**defaults, **environ}
    try:
        assert_environment(MANIFEST, environ=environ, require_injected=True)
    except RuntimeError as refusal:
        return str(refusal).split("declares: ")[1].split(" --")[0].split(", ")
    return []


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_the_promote_environment_has_every_declared_name(env: str) -> None:
    assert refused_names(backend_environ(env)) == []


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
