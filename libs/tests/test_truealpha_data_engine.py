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


def test_moomoo_origin_flags_are_on_in_staging_and_production_only():
    """truealpha#854: the moomoo K-line and statements origins are feature-flagged in the
    engine. They soaked on staging, were proven on a forced staging tick (truealpha#874),
    and production turned them on through a reviewed change here — the flags are part of
    the public env, so each flip is a new CONFIGURATION_SHA256, never an unreviewed env
    edit. Any other environment name stays off."""
    module = _load_deploy_module()
    deployer = module.DataEngineDeployer
    for environment in ("staging", "production"):
        env = deployer._release_recomputable_env(environment)
        for flag in ("MOOMOO_KLINE_ORIGIN_ENABLED", "MOOMOO_FINANCIALS_ORIGIN_ENABLED"):
            assert env[flag] == "true", (environment, flag)
    other = deployer._release_recomputable_env("pr-999")
    for flag in ("MOOMOO_KLINE_ORIGIN_ENABLED", "MOOMOO_FINANCIALS_ORIGIN_ENABLED"):
        assert other[flag] == "false", flag
    compose = yaml.safe_load((SERVICE_DIR / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    for name in ("dagster-code-server", "dagster-daemon", "dagster-webserver"):
        environment = services[name]["environment"]
        for flag in ("MOOMOO_KLINE_ORIGIN_ENABLED", "MOOMOO_FINANCIALS_ORIGIN_ENABLED"):
            assert environment.get(flag) == "${" + flag + ":-false}", (name, flag)


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
        deployer,
        "secrets_backend",
        classmethod(lambda cls, env=None: _Secrets(values)),
    )
    assert not deployer.ensure_runtime_secrets()


def _load_s3_deploy_module():
    spec = importlib.util.spec_from_file_location(
        "platform_s3_deploy", ROOT / "platform/03.s3/deploy.py"
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
    published = _load_s3_deploy_module().S3Deployer._S3_HOST_PORTS
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
    # Since #637 the template is generated from the app manifest and carries no
    # S3_ENDPOINT at all; the compose anchor derives it from the published port.
    template = (SERVICE_DIR / "secrets.ctmpl").read_text()
    assert ".Data.data.S3_ENDPOINT" not in template, (
        "S3_ENDPOINT must not come from Vault"
    )
    compose = (SERVICE_DIR / "compose.yaml").read_text()
    assert "S3_ENDPOINT: http://127.0.0.1:${TA_MINIO_S3_PORT}" in compose
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


def test_pin_release_retries_a_lookup_that_got_no_answer(monkeypatch):
    """2026-09-17: the v0.0.83 staging deploy died on one dropped ghcr.io connection
    ("Remote end closed connection without response") with every other step green. A
    lookup that got no answer is retried; the pin then proceeds as usual."""
    import http.client

    deploy = _load_deploy_module()
    deployer = deploy.DataEngineDeployer
    secrets = _WritableSecrets(_secret_values("a"))
    monkeypatch.setattr(deploy, "error", lambda *_a, **_k: None)
    monkeypatch.setattr(deploy, "success", lambda *_a, **_k: None)
    digest = "sha256:" + "d" * 64
    failures = [
        http.client.RemoteDisconnected("Remote end closed connection without response"),
        TimeoutError("timed out"),
    ]

    def resolve(image, ref):
        if failures:
            raise failures.pop(0)
        return digest

    slept: list[float] = []
    assert (
        deployer.pin_release(
            "v0.0.83", secrets=secrets, resolve=resolve, sleep=slept.append
        )
        == digest
    )
    assert slept == list(deploy._PIN_RESOLVE_BACKOFF_SECONDS)
    assert ("DATA_ENGINE_IMAGE_DIGEST", digest) in secrets.writes


def test_pin_release_gives_up_after_the_last_transport_failure(monkeypatch):
    import pytest

    deploy = _load_deploy_module()
    deployer = deploy.DataEngineDeployer
    secrets = _WritableSecrets(_secret_values("a"))
    monkeypatch.setattr(deploy, "error", lambda *_a, **_k: None)
    calls: list[str] = []

    def resolve(image, ref):
        calls.append(ref)
        raise ConnectionResetError("reset by peer")

    with pytest.raises(ConnectionResetError):
        deployer.pin_release(
            "v0.0.83", secrets=secrets, resolve=resolve, sleep=lambda _s: None
        )
    assert len(calls) == len(deploy._PIN_RESOLVE_BACKOFF_SECONDS) + 1
    assert secrets.writes == []


def test_pin_release_never_retries_a_registry_answer(monkeypatch):
    """A 404 or a refusal is an answer: retrying cannot change it, so it fails at once."""
    import pytest
    from infra2_sdk import release

    deploy = _load_deploy_module()
    deployer = deploy.DataEngineDeployer
    secrets = _WritableSecrets(_secret_values("a"))
    calls: list[str] = []

    def resolve(image, ref):
        calls.append(ref)
        raise release.ReleaseError("does not exist in the registry")

    with pytest.raises(release.ReleaseError):
        deployer.pin_release(
            "v9.9.9", secrets=secrets, resolve=resolve, sleep=lambda _s: None
        )
    assert calls == ["v9.9.9"]
    assert secrets.writes == []


def test_ensure_runtime_secrets_refuses_the_deploy_when_the_pin_fails(monkeypatch):
    deploy = _load_deploy_module()
    deployer = deploy.DataEngineDeployer
    secrets = _WritableSecrets(_secret_values("a"))
    monkeypatch.setattr(
        deployer,
        "secrets_backend",
        classmethod(lambda cls, env=None: secrets),
    )
    monkeypatch.setattr(deployer, "env", classmethod(lambda cls: {"ENV": "staging"}))
    monkeypatch.setenv("DEPLOY_VERSION_REF", "v0.0.46")
    monkeypatch.setattr(deploy, "error", lambda *_a, **_k: None)
    from infra2_sdk import release

    monkeypatch.setattr(
        release,
        "resolve_image_digest",
        lambda **kw: (_ for _ in ()).throw(release.ReleaseError("does not exist")),
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
    monkeypatch.setattr(
        deployer,
        "secrets_backend",
        classmethod(lambda cls, env=None: secrets),
    )
    monkeypatch.setattr(deployer, "env", classmethod(lambda cls: {"ENV": "staging"}))
    monkeypatch.setenv("DEPLOY_VERSION_REF", "v0.0.47")
    from infra2_sdk import release

    monkeypatch.setattr(
        release, "resolve_image_digest", lambda **kw: "sha256:" + "9" * 64
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


def test_ensure_runtime_secrets_accepts_env_parameter(monkeypatch):
    """Issue #803: DataEngineDeployer.ensure_runtime_secrets must accept env parameter
    and pass it to secrets_backend without falling back to production."""
    deploy = _load_deploy_module()
    deployer = deploy.DataEngineDeployer
    secrets = _WritableSecrets(_secret_values("a"))
    calls: list[str | None] = []

    def mock_secrets_backend(env=None):
        calls.append(env)
        return secrets

    monkeypatch.setattr(
        deployer,
        "secrets_backend",
        classmethod(lambda cls, env=None: mock_secrets_backend(env)),
    )
    monkeypatch.setenv("DEPLOY_VERSION_REF", "v0.0.48")
    from infra2_sdk import release

    monkeypatch.setattr(
        release, "resolve_image_digest", lambda **kw: "sha256:" + "8" * 64
    )
    monkeypatch.setattr(deploy, "success", lambda *_a, **_k: None)

    # Calling with env="staging" must succeed and not raise TypeError
    assert deployer.ensure_runtime_secrets(env="staging") is True
    assert "staging" in calls
    assert ("GIT_COMMIT_SHA", "v0.0.48") in secrets.writes


def test_verify_runtime_applied_passes_timeout_to_inspect(monkeypatch):
    """Issue #803: verify_runtime_applied must pass -o BatchMode=yes -o ConnectTimeout=10
    to ssh, and timeout=15 to inspect calls in both loops."""
    deploy = _load_deploy_module()
    deployer = deploy.DataEngineDeployer
    monkeypatch.setattr(
        deployer,
        "env",
        classmethod(lambda cls: {"VPS_HOST": "1.2.3.4", "VPS_SSH_USER": "root"}),
    )

    calls: list[tuple[str, int | None]] = []

    class MockResult:
        ok = True
        stdout = "ghcr.io/wangzitian0/truealpha-data-engine@sha256:" + "a" * 64
        stderr = ""

    class MockContext:
        def run(self, cmd, warn=True, hide=True, timeout=None):
            calls.append((cmd, timeout))
            res = MockResult()
            if "Health.Status" in cmd:
                res.stdout = "healthy"
            return res

    mock_c = MockContext()
    digest = "sha256:" + "a" * 64
    err = deployer.verify_runtime_applied(mock_c, {"DATA_ENGINE_IMAGE_DIGEST": digest})
    assert err is None

    # remote() commands must include ssh hardening flags
    for cmd, timeout in calls:
        assert "-o BatchMode=yes" in cmd
        assert "-o ConnectTimeout=10" in cmd

    # inspect calls must pass timeout=15
    inspect_calls = [c for c in calls if "docker inspect" in c[0]]
    assert len(inspect_calls) == 6, (
        f"expected 6 inspect calls (3 image + 3 health), got {len(inspect_calls)}"
    )
    for cmd, timeout in inspect_calls:
        assert (
            timeout == 15
        ), f"expected timeout=15 for inspect call, got {timeout}: {cmd}"


def test_verify_runtime_applied_retries_on_command_timed_out(monkeypatch):
    """Issue #803: when docker inspect raises CommandTimedOut, verify_runtime_applied
    should handle it gracefully without crashing, retrying until deadline."""
    from invoke.exceptions import CommandTimedOut

    deploy = _load_deploy_module()
    deployer = deploy.DataEngineDeployer
    monkeypatch.setattr(
        deployer,
        "env",
        classmethod(lambda cls: {"VPS_HOST": "1.2.3.4", "VPS_SSH_USER": "root"}),
    )
    # Shorten deadlines for test speed
    monkeypatch.setattr(deployer, "SWITCH_DEADLINE_SECONDS", 0.05)
    monkeypatch.setattr(deployer, "POLL_INTERVAL_SECONDS", 0.01)

    class MockContext:
        def run(self, cmd, warn=True, hide=True, timeout=None):
            if "docker pull" in cmd:
                class PullResult:
                    ok = True
                    stdout = "pull success"
                    stderr = ""

                return PullResult()
            # docker inspect raises CommandTimedOut
            raise CommandTimedOut("inspect command timed out", timeout=15)

    mock_c = MockContext()
    digest = "sha256:" + "a" * 64
    err = deployer.verify_runtime_applied(mock_c, {"DATA_ENGINE_IMAGE_DIGEST": digest})
    assert err is not None
    assert "promoted image digest was not applied" in err
    assert "unavailable" in err


