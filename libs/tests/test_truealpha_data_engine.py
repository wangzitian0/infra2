import importlib.util
from pathlib import Path

import yaml

from libs import service_registry

ROOT = Path(__file__).resolve().parents[2]
SERVICE_DIR = ROOT / "truealpha/truealpha/20.data_engine"


def _load_deploy_module():
    spec = importlib.util.spec_from_file_location("truealpha_data_engine_deploy", SERVICE_DIR / "deploy.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Secrets:
    def __init__(self, values):
        self.values = values

    def get(self, key):
        return self.values.get(key)


def _secret_values(digest_char="a"):
    return {
        "SEC_USER_AGENT": "TrueAlpha test test@example.com",
        "S3_ENDPOINT": "https://s3-staging.example.test",
        "S3_ACCESS_KEY": "test-access",
        "S3_SECRET_KEY": "test-secret",
        "S3_BUCKET": "truealpha-raw-staging",
        "DATA_ENGINE_IMAGE_DIGEST": "sha256:" + digest_char * 64,
        "RELEASE_MANIFEST_ID": "release-manifest:" + "b" * 64,
        "CAPTURE_APPROVED_BY": "review:test",
        "GIT_COMMIT_SHA": "c" * 40,
    }


def test_service_is_registry_discovered_and_not_public():
    metadata = service_registry.service_attrs()["truealpha/data_engine"]
    assert metadata.project == "truealpha"
    assert metadata.subdomain is None
    assert metadata.service_name == "dagster-webserver"


def test_compose_pins_one_digest_and_keeps_dagster_on_host_loopback():
    compose = yaml.safe_load((SERVICE_DIR / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    expected_image = (
        "ghcr.io/wangzitian0/truealpha-data-engine@${DATA_ENGINE_IMAGE_DIGEST:?DATA_ENGINE_IMAGE_DIGEST is required}"
    )
    for name in ("dagster-webserver", "dagster-daemon"):
        service = services[name]
        assert service["image"] == expected_image
        assert service["network_mode"] == "host"
        assert "ports" not in service
        assert service["mem_limit"]
        assert service["cpu_shares"]
        assert "traefik.enable=false" in service["labels"]
    web_command = services["dagster-webserver"]["command"]
    assert "127.0.0.1" in web_command
    assert "${DAGSTER_WEBSERVER_PORT}" in web_command


def test_deployer_derives_isolated_ports_and_full_configuration_hash(monkeypatch):
    module = _load_deploy_module()
    deployer = module.DataEngineDeployer
    values = _secret_values()
    monkeypatch.setattr(deployer, "secrets", classmethod(lambda cls: _Secrets(values)))
    environment = {
        "ENV": "staging",
        "ENV_SUFFIX": "-staging",
        "ENV_DOMAIN_SUFFIX": "-staging",
        "INTERNAL_DOMAIN": "example.test",
    }
    config = deployer.compose_env_base(environment)
    assert config["DATA_PATH"] == "/data/truealpha/dagster-staging"
    assert config["TA_POSTGRES_PORT"] == "15432"
    assert config["DAGSTER_WEBSERVER_PORT"] == "13001"
    assert config["DATA_ENGINE_IMAGE_DIGEST"] == values["DATA_ENGINE_IMAGE_DIGEST"]
    assert len(config["CONFIGURATION_SHA256"]) == 64

    values["DATA_ENGINE_IMAGE_DIGEST"] = "sha256:" + "d" * 64
    changed = deployer.compose_env_base(environment)
    assert changed["CONFIGURATION_SHA256"] != config["CONFIGURATION_SHA256"]


def test_deployer_fails_closed_on_missing_or_malformed_release_inputs(monkeypatch):
    module = _load_deploy_module()
    deployer = module.DataEngineDeployer
    values = _secret_values()
    values["DATA_ENGINE_IMAGE_DIGEST"] = "latest"
    monkeypatch.setattr(deployer, "secrets", classmethod(lambda cls: _Secrets(values)))
    assert not deployer.ensure_runtime_secrets()
