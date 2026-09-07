import importlib.util
import re
from pathlib import Path

import yaml

from libs import service_registry

ROOT = Path(__file__).resolve().parents[2]
SERVICE_DIR = ROOT / "truealpha/truealpha/20.data_engine"


def _load_deploy_module():
    spec = importlib.util.spec_from_file_location(
        "truealpha_data_engine_deploy", SERVICE_DIR / "deploy.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_shared_tasks_module():
    spec = importlib.util.spec_from_file_location(
        "truealpha_data_engine_shared_tasks", SERVICE_DIR / "shared_tasks.py"
    )
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


def test_status_health_commands_are_safe_for_remote_single_quote_wrapper():
    module = _load_shared_tasks_module()

    assert "'" not in module.WEBSERVER_HEALTH_COMMAND
    assert "'" not in module.DAEMON_HEALTH_COMMAND
    assert "'" not in module.CODE_SERVER_HEALTH_COMMAND
    assert '\\"http://127.0.0.1:\\"' in module.WEBSERVER_HEALTH_COMMAND
    assert "\\$DATABASE_URL" in module.DAEMON_HEALTH_COMMAND
    assert "/var/lib/dagster/code-server.sock" in module.CODE_SERVER_HEALTH_COMMAND


def test_compose_pins_one_digest_and_keeps_dagster_on_host_loopback():
    compose = yaml.safe_load((SERVICE_DIR / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    expected_image = "ghcr.io/wangzitian0/truealpha-data-engine@${DATA_ENGINE_IMAGE_DIGEST:?DATA_ENGINE_IMAGE_DIGEST is required}"
    for name in ("dagster-webserver", "dagster-daemon", "dagster-code-server"):
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


def test_webserver_and_daemon_load_the_persistent_code_server_over_grpc_socket():
    # Eliminates the ~68-70s heartbeat-timeout churn each role's own "managed"
    # local code-server subprocess produced under -m data_engine.dagster_defs
    # (dagster._daemon.controller.DAEMON_GRPC_SERVER_HEARTBEAT_TTL = 20) by
    # pointing both roles at one long-lived dagster-code-server instead.
    compose = yaml.safe_load((SERVICE_DIR / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]

    code_server_command = services["dagster-code-server"]["command"]
    assert "--socket" in code_server_command
    assert "/var/lib/dagster/code-server.sock" in code_server_command
    assert "--heartbeat" not in code_server_command

    for name in ("dagster-webserver", "dagster-daemon"):
        service = services[name]
        assert "--grpc-socket" in service["command"]
        assert "/var/lib/dagster/code-server.sock" in service["command"]
        assert "-m" not in service["command"]
        assert service["depends_on"]["dagster-code-server"] == {
            "condition": "service_healthy"
        }


def test_deployer_derives_isolated_ports_and_full_configuration_hash(monkeypatch):
    module = _load_deploy_module()
    deployer = module.DataEngineDeployer
    values = _secret_values()
    monkeypatch.setattr(
        deployer, "secrets_backend", classmethod(lambda cls: _Secrets(values))
    )
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


def test_source_identity_is_release_recomputable_without_vault(monkeypatch):
    module = _load_deploy_module()
    deployer = module.DataEngineDeployer
    monkeypatch.setattr(
        deployer,
        "secrets_backend",
        classmethod(lambda cls: (_ for _ in ()).throw(AssertionError("Vault read"))),
    )
    environment = {
        "ENV": "production",
        "ENV_SUFFIX": "",
        "ENV_DOMAIN_SUFFIX": "",
        "INTERNAL_DOMAIN": "example.test",
    }

    source = deployer.source_config_env_base(environment)

    assert source["TA_POSTGRES_PORT"] == "15433"
    assert source["TA_MINIO_S3_PORT"] == "19001"
    assert source["DAGSTER_WEBSERVER_PORT"] == "13002"
    assert deployer.runtime_only_config_keys
    assert not deployer.runtime_only_config_keys.intersection(source)


def test_deployer_fails_closed_on_missing_or_malformed_release_inputs(monkeypatch):
    module = _load_deploy_module()
    deployer = module.DataEngineDeployer
    values = _secret_values()
    values["DATA_ENGINE_IMAGE_DIGEST"] = "latest"
    monkeypatch.setattr(
        deployer, "secrets_backend", classmethod(lambda cls: _Secrets(values))
    )
    assert not deployer.ensure_runtime_secrets()


def _load_minio_deploy_module():
    spec = importlib.util.spec_from_file_location(
        "platform_minio_deploy", ROOT / "platform/03.minio/deploy.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_dialled_s3_port_matches_the_one_minio_publishes():
    """These two constants live in independently-deployed stacks, so drift is the
    realistic failure — and #602 already demonstrated half of it: MinIO began publishing
    19000/19001 while this service kept dialling the 9000 baked into Vault, so capture
    stayed broken after the publish landed.
    """
    published = _load_minio_deploy_module().MinioDeployer._S3_HOST_PORTS
    dialled = _load_deploy_module().DataEngineDeployer._MINIO_S3_PORTS
    assert set(published) == set(dialled)
    for env, addr in published.items():
        assert addr.endswith(":" + dialled[env]), (
            f"{env}: dialling :{dialled[env]}, MinIO publishes {addr}"
        )


def _vault_agent_environment() -> dict[str, str]:
    compose = yaml.safe_load((SERVICE_DIR / "compose.yaml").read_text())
    return compose["services"]["vault-agent"]["environment"]


def test_vault_agent_forwards_every_template_env():
    """`env "X"` in secrets.ctmpl reads the vault-agent container's own environment,
    not the Compose project env. #604 added `env "TA_MINIO_S3_PORT"` to the template
    while compose.yaml kept forwarding only TA_POSTGRES_PORT, so a recreated agent
    rendered S3_ENDPOINT=http://127.0.0.1: (no port) and every raw capture failed
    with `cannot access bucket truealpha-raw` (staging, 2026-09-07).
    """
    template = (SERVICE_DIR / "secrets.ctmpl").read_text()
    referenced = set(re.findall(r'env "([A-Z0-9_]+)"', template))
    assert referenced, "template no longer reads any env — update this test"
    forwarded = set(_vault_agent_environment())
    missing = sorted(referenced - forwarded)
    assert not missing, (
        f"secrets.ctmpl reads {missing} but vault-agent does not forward them"
    )
    assert _vault_agent_environment()["TA_MINIO_S3_PORT"] == "${TA_MINIO_S3_PORT}"


def test_vault_agent_is_recreated_when_its_templates_change():
    """secrets.ctmpl is a single-file bind mount: a checkout that rewrites the file
    gives it a new inode, the running agent keeps the old one, and Compose only
    recreates the agent when its resolved config changes. CONFIGURATION_SHA256
    (compose.yaml + secrets.ctmpl + vault-agent.hcl + vault-policy.hcl + public env)
    must therefore be part of the agent's config, as it already is for the runtime
    services — otherwise template edits ship without ever rendering (#604, #626).
    """
    environment = _vault_agent_environment()
    assert environment.get("TRUEALPHA_CONFIGURATION_SHA256", "").startswith(
        "${CONFIGURATION_SHA256"
    ), "vault-agent must carry CONFIGURATION_SHA256 so template changes recreate it"
    hashed = _load_deploy_module().DataEngineDeployer
    assert callable(getattr(hashed, "_configuration_sha256", None))


def test_s3_endpoint_is_derived_not_taken_from_vault():
    """A Vault-supplied endpoint is what produced the outage: the stored value was written
    for host-side sweep scripts, and nothing in the deploy could tell it was wrong for a
    `network_mode: host` container."""
    template = (SERVICE_DIR / "secrets.ctmpl").read_text()
    assert 'env "TA_MINIO_S3_PORT"' in template
    assert ".Data.data.S3_ENDPOINT" not in template, (
        "S3_ENDPOINT must not come from Vault"
    )
    assert (
        "S3_ENDPOINT"
        not in _load_deploy_module().DataEngineDeployer._REQUIRED_SECRET_KEYS
    )


class _WritableSecrets(_Secrets):
    def __init__(self, values):
        super().__init__(values)
        self.writes: list[tuple[str, str]] = []

    def set(self, key, value):
        self.writes.append((key, value))
        self.values[key] = value
        return True


def test_a_release_request_pins_the_tag_digest_in_vault_before_reading_it(monkeypatch):
    """truealpha#712: the runner resolves truealpha-data-engine:<tag> to its registry digest
    and writes it (with the tag as GIT_COMMIT_SHA, the identifier the app images stamp)
    before compose_env_base reads Vault. The existing verify_runtime_applied then proves
    the containers run that digest."""
    deploy = _load_deploy_module()
    deployer = deploy.DataEngineDeployer
    secrets = _WritableSecrets(_secret_values("a"))
    monkeypatch.setattr(deployer, "secrets_backend", classmethod(lambda cls: secrets))
    monkeypatch.setattr(deployer, "env", classmethod(lambda cls: {"ENV": "staging"}))
    # The runner passes the release in the child PROCESS environment; Deployer.env()
    # is the curated config and never carries it (the 2026-09-07 silent skip).
    monkeypatch.setenv("DEPLOY_VERSION_REF", "v0.0.46")
    new_digest = "sha256:" + "e" * 64
    asked: list[tuple[str, str]] = []

    def resolve(image, ref):
        asked.append((image, ref))
        return new_digest

    assert (
        deployer.pin_release("v0.0.46", secrets=secrets, resolve=resolve) == new_digest
    )
    assert asked == [("ghcr.io/wangzitian0/truealpha-data-engine", "v0.0.46")]
    assert secrets.writes == [
        ("DATA_ENGINE_IMAGE_DIGEST", new_digest),
        ("GIT_COMMIT_SHA", "v0.0.46"),
    ]
    # compose_env_base now reads the pinned values, not the ones Vault held before
    env = deployer.compose_env_base(
        {"ENV": "staging", "DATA_PATH": "/tmp/x", "ENV_SUFFIX": "-staging"}
    )
    assert (
        env["DATA_ENGINE_IMAGE_DIGEST"] == new_digest
        and env["GIT_COMMIT_SHA"] == "v0.0.46"
    )


def test_pin_release_fails_closed_on_a_bad_ref_or_an_unresolvable_tag(monkeypatch):
    import pytest

    deploy = _load_deploy_module()
    deployer = deploy.DataEngineDeployer
    secrets = _WritableSecrets(_secret_values("a"))
    with pytest.raises(ValueError, match="vX.Y.Z tag or a commit sha"):
        deployer.pin_release(
            "latest", secrets=secrets, resolve=lambda image, ref: "sha256:" + "f" * 64
        )
    with pytest.raises(RuntimeError, match="no such tag"):
        deployer.pin_release(
            "v9.9.9",
            secrets=secrets,
            resolve=lambda image, ref: (_ for _ in ()).throw(
                RuntimeError("no such tag")
            ),
        )
    assert secrets.writes == []


def test_ensure_runtime_secrets_refuses_the_deploy_when_the_pin_fails(monkeypatch):
    deploy = _load_deploy_module()
    deployer = deploy.DataEngineDeployer
    secrets = _WritableSecrets(_secret_values("a"))
    monkeypatch.setattr(deployer, "secrets_backend", classmethod(lambda cls: secrets))
    monkeypatch.setattr(deployer, "env", classmethod(lambda cls: {"ENV": "staging"}))
    monkeypatch.setenv("DEPLOY_VERSION_REF", "v0.0.46")
    monkeypatch.setattr(deploy, "error", lambda *_a, **_k: None)
    import libs.image_digest as image_digest

    monkeypatch.setattr(
        image_digest,
        "resolve_image_digest",
        lambda image, ref: (_ for _ in ()).throw(
            image_digest.ImageDigestError("does not exist")
        ),
    )
    assert deployer.ensure_runtime_secrets() is False
    assert secrets.writes == []


def test_ensure_runtime_secrets_pins_from_the_process_environment(monkeypatch):
    """The regression that shipped: the version ref was read from cls.env(), which is
    the curated deployment config and never carries DEPLOY_VERSION_REF, so the runner
    reported success with no pin. It must come from os.environ."""
    deploy = _load_deploy_module()
    deployer = deploy.DataEngineDeployer
    secrets = _WritableSecrets(_secret_values("a"))
    monkeypatch.setattr(deployer, "secrets_backend", classmethod(lambda cls: secrets))
    monkeypatch.setattr(deployer, "env", classmethod(lambda cls: {"ENV": "staging"}))
    monkeypatch.setenv("DEPLOY_VERSION_REF", "v0.0.47")
    import libs.image_digest as image_digest

    monkeypatch.setattr(
        image_digest, "resolve_image_digest", lambda image, ref: "sha256:" + "9" * 64
    )
    monkeypatch.setattr(deploy, "success", lambda *_a, **_k: None)
    assert deployer.ensure_runtime_secrets() is True
    assert ("GIT_COMMIT_SHA", "v0.0.47") in secrets.writes


def test_code_server_healthcheck_is_not_a_dagster_cli_cold_start():
    """2026-09-07: `dagster api grpc-health-check` cost 10–11 s of CPU per check (a full
    dagster import) every 30 s with a 10 s timeout, so under host load both code servers
    were permanently unhealthy and the checks themselves were a third of a core each.
    The probe must stay a bare socket connect — no dagster CLI, no dagster import."""
    compose = yaml.safe_load((SERVICE_DIR / "compose.yaml").read_text())
    check = compose["services"]["dagster-code-server"]["healthcheck"]
    # The shape, not substrings (review on #636): the executable is bare python, and no
    # token of the command is a dagster CLI entry point of any kind.
    assert check["test"][:2] == ["CMD", "python"]
    assert not any(token.startswith("dagster") for token in check["test"][2:])
    command = " ".join(check["test"])
    assert (
        "socket.AF_UNIX" in command and "/var/lib/dagster/code-server.sock" in command
    )
    assert "import dagster" not in command
    interval = int(str(check["interval"]).rstrip("s"))
    timeout = int(str(check["timeout"]).rstrip("s"))
    assert interval >= 60 and timeout <= interval // 2
