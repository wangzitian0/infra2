"""Every platform service that calls `init_service` ships OpenTelemetry end to end (#1209).

`init_service(..., enable_otel=True)` starts export only when four links hold in the
running container:

1. The deploy issues the identity.
2. The compose passes the identity and the OTLP endpoint to the app.
3. The image has the OpenTelemetry packages.
4. The app starts telemetry only through `init_service`.

The bridge of `platform/12.alerting` called `init_service` for months with links 1 to 3
missing. OpenTelemetry stayed off without an error: 0 spans and 0 logs in SigNoz.

These tests find each caller in the source and check all four links for each one. A new
caller needs no new test.
"""

from __future__ import annotations

import ast
import importlib.metadata
import importlib.util
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from packaging.requirements import Requirement

from libs.core import registry as reg
from libs.deploy.deployer import Deployer
from libs.tests.compose_env import compose_services, container_env, raw_environment

ROOT = Path(__file__).resolve().parents[2]
PLATFORM = ROOT / "platform"
COLLECTOR_OTLP_HTTP = "http://platform-signoz-otel-collector:4318"
#: The callers that exist today. They prove that the scanner finds something.
KNOWN_CALLERS = {"platform/12.alerting/app.py", "platform/30.todo/app.py"}


def _callee_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def calls_named(source: str, names: set[str]) -> list[ast.Call]:
    """The calls in `source` to one of `names`. A comment or a string is not a call."""
    return [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and _callee_name(node) in names
    ]


def init_service_calls(source: str) -> list[ast.Call]:
    return calls_named(source, {"init_service"})


def _callers() -> list[Path]:
    return sorted(
        path
        for path in PLATFORM.rglob("*.py")
        if "init_service" in path.read_text(encoding="utf-8")
        and init_service_calls(path.read_text(encoding="utf-8"))
    )


CALLERS = _callers()
CALLER_IDS = [str(path.relative_to(ROOT)) for path in CALLERS]


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _passed_name(app: Path) -> str:
    (call,) = init_service_calls(app.read_text(encoding="utf-8"))
    value = _keyword(call, "name")
    assert isinstance(value, ast.Constant) and isinstance(value.value, str), (
        f"{app}: init_service needs a literal name= keyword"
    )
    return value.value


def _meta(app: Path):
    compose = f"{app.parent.relative_to(ROOT).as_posix()}/compose.yaml"
    (meta,) = [m for m in reg.service_attrs().values() if m.compose_path == compose]
    return meta


def _app_service(app: Path) -> str:
    """The compose service that builds the image of this app."""
    dockerfile = f"{app.parent.relative_to(ROOT).as_posix()}/Dockerfile"
    (name,) = [
        name
        for name, service in compose_services(app.parent / "compose.yaml").items()
        if (service.get("build") or {}).get("dockerfile") == dockerfile
    ]
    return name


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------ the scanner itself


def test_the_scanner_finds_every_known_caller() -> None:
    assert KNOWN_CALLERS <= set(CALLER_IDS), CALLER_IDS


def test_the_scanner_tells_a_call_from_a_mention() -> None:
    assert len(init_service_calls("init_service(app, name='a')")) == 1
    assert len(init_service_calls("import sdk\nsdk.init_service(app, name='a')")) == 1
    assert init_service_calls("# init_service(app)\nx = 'init_service(app)'") == []
    assert init_service_calls("from infra2_sdk.fastapi import init_service") == []


#: Each of these starts a second OpenTelemetry bootstrap next to the one in `init_service`.
OWN_BOOTSTRAPS = {"configure_telemetry", "configure_service_telemetry"}


def test_the_scanner_tells_a_second_bootstrap_from_a_mention() -> None:
    own = "from infra2_sdk.runtime import otel\notel.configure_telemetry(settings)"
    assert len(calls_named(own, OWN_BOOTSTRAPS)) == 1
    assert len(calls_named("configure_service_telemetry()", OWN_BOOTSTRAPS)) == 1
    assert (
        calls_named(
            "# configure_telemetry()\nx = 'configure_telemetry()'", OWN_BOOTSTRAPS
        )
        == []
    )


@pytest.mark.parametrize("app", CALLERS, ids=CALLER_IDS)
def test_the_app_starts_telemetry_only_through_init_service(app: Path) -> None:
    """The todo app once called `init_service` at import and `configure_telemetry` from
    `main()`. The second call logged a RuntimeWarning at every start (#1209)."""
    own = calls_named(app.read_text(encoding="utf-8"), OWN_BOOTSTRAPS)
    assert own == [], f"{app}: lines {[call.lineno for call in own]}"


# ------------------------------------------------------------------ link 1: the deploy


@pytest.mark.parametrize("app", CALLERS, ids=CALLER_IDS)
def test_the_deployer_issues_the_name_the_app_passes_to_init_service(app: Path) -> None:
    meta = _meta(app)
    assert meta.telemetry_service_name == _passed_name(app), (
        f"{meta.service_id}: Deployer.telemetry_service_name must equal the name= of "
        "init_service. Without it the deploy issues no OTEL_SERVICE_NAME."
    )


# ------------------------------------------------------------------ link 3: the image


def _otel_extra_requirements() -> list[Requirement]:
    declared = [
        Requirement(requirement.split(";")[0].strip())
        for requirement in (importlib.metadata.requires("infra2-sdk") or [])
        if 'extra == "otel"' in requirement
    ]
    assert declared, "the installed SDK declares no otel extra"
    return declared


@pytest.mark.parametrize("app", CALLERS, ids=CALLER_IDS)
def test_the_image_installs_the_sdk_otel_extra_requirements_as_plain_pins(
    app: Path,
) -> None:
    """pyproject keeps `infra2-sdk @ <wheel>` extras-free, so the Dockerfile lists the
    `otel` extra's requirements itself. They must equal what the SDK declares."""
    dockerfile = (app.parent / "Dockerfile").read_text(encoding="utf-8")
    for parsed in _otel_extra_requirements():
        pins = re.findall(rf'"({re.escape(parsed.name)}[^"\s]*)"', dockerfile)
        assert pins, f"{app.parent.name}/Dockerfile lacks {parsed.name}"
        installed_spec = Requirement(pins[0]).specifier
        assert installed_spec == parsed.specifier, (
            f"{parsed.name}: Dockerfile pins {installed_spec}, SDK needs "
            f"{parsed.specifier}"
        )


# ------------------------------------------------------------------ link 2: the compose


@pytest.mark.parametrize("app", CALLERS, ids=CALLER_IDS)
def test_the_compose_passes_the_endpoint_and_the_tier_to_the_app(app: Path) -> None:
    service = compose_services(app.parent / "compose.yaml")[_app_service(app)]
    environment = raw_environment(service)
    assert environment.get("OTEL_EXPORTER_OTLP_ENDPOINT") == COLLECTOR_OTLP_HTTP
    # the SDK resolves the environment tier from ENV when it configures telemetry
    assert environment.get("ENV") == "${ENV}"


@pytest.mark.parametrize("app", CALLERS, ids=CALLER_IDS)
def test_the_compose_hands_the_container_whatever_identity_the_deploy_issues(
    app: Path,
) -> None:
    """The deploy is the only issuer. A hardcoded or defaulted name would pass the sync
    test below while the container ignored what the deploy issued."""
    issued = {
        "ENV": "staging",
        "OTEL_SERVICE_NAME": "issued-by-the-deploy",
        "OTEL_RESOURCE_ATTRIBUTES": "service.name=issued-by-the-deploy,k=v",
    }
    service = _app_service(app)
    container = container_env(app.parent / "compose.yaml", service, issued)
    assert container["OTEL_SERVICE_NAME"] == "issued-by-the-deploy"
    assert container["OTEL_RESOURCE_ATTRIBUTES"] == issued["OTEL_RESOURCE_ATTRIBUTES"]
    bare = container_env(app.parent / "compose.yaml", service, {"ENV": "staging"})
    assert bare["OTEL_SERVICE_NAME"] == "" and bare["OTEL_RESOURCE_ATTRIBUTES"] == "", (
        "with no issued identity the container must get none"
    )


# ------------------------------------------------------------------ links 1 and 2 joined


class _Matches:
    def __eq__(self, other):
        return True

    def __ne__(self, other):
        return False

    __hash__ = object.__hash__


class _Remote(dict):
    def __init__(self, release: str):
        super().__init__()
        self.release = release

    def __getitem__(self, key):
        return {"runtime_hash": "old", "deploy_ref": self.release}.get(key, _Matches())

    def get(self, key, default=None):
        return self[key]


class _NoVaultSecrets:
    """The alerting deployer reads two heartbeat values from Vault. A test has no Vault."""

    def get(self, key):
        return None


@pytest.mark.parametrize("app", CALLERS, ids=CALLER_IDS)
def test_the_deployer_issues_the_identity_and_the_compose_hands_it_to_the_container(
    app: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run the real `Deployer.sync` and resolve the compose against the env it pushes.

    Asserting the deploy-side value alone proves nothing reaches the container, and
    asserting the compose text alone proves a reference. Only the join does.
    """
    import libs.deploy.deployer as d

    module = _load(
        f"adoption_deploy_{app.parent.name.replace('.', '_')}", app.parent / "deploy.py"
    )
    (deployer,) = [
        value
        for value in vars(module).values()
        if isinstance(value, type)
        and issubclass(value, Deployer)
        and value is not Deployer
    ]
    meta = _meta(app)
    pushed: dict[str, str] = {}
    release = "c" * 40

    def composing(cls, c, env_vars):
        pushed.update(env_vars)
        return "compose-1"

    for name, value in {
        "env": classmethod(
            lambda cls: {
                "ENV": "staging",
                "ENV_SUFFIX": "-staging",
                "ENV_DOMAIN_SUFFIX": "-staging",
                "INTERNAL_DOMAIN": "zitian.party",
                "VPS_HOST": "vps",
            }
        ),
        "verify_vault_app_token": classmethod(lambda cls: {"valid": True}),
        "ensure_runtime_secrets": classmethod(lambda cls, c, env=None: True),
        "apply_secret_supply": classmethod(lambda cls, c, env=None: True),
        "compute_local_config_hash": classmethod(lambda cls, c, env: "new"),
        "get_remote_config_identity": classmethod(lambda cls: _Remote(release)),
        "_await_effective_config_hash": classmethod(lambda cls, h: "new"),
        "composing": classmethod(composing),
        "verify_runtime_applied": classmethod(lambda cls, c, env: None),
        "verify_in_service": classmethod(lambda cls, c, cid: None),
        "restart_dependents": classmethod(lambda cls, c, e: []),
    }.items():
        monkeypatch.setattr(deployer, name, value)
    if hasattr(module, "get_secrets"):
        monkeypatch.setattr(module, "get_secrets", lambda **kwargs: _NoVaultSecrets())
    monkeypatch.setattr(d, "validate_env", lambda: [])
    monkeypatch.setenv("IAC_DEPLOY_REF", release)

    result = deployer.sync(MagicMock())
    assert result["action"] in ("created", "updated"), result

    container = container_env(app.parent / "compose.yaml", _app_service(app), pushed)
    assert container["OTEL_EXPORTER_OTLP_ENDPOINT"] == COLLECTOR_OTLP_HTTP
    assert container["OTEL_SERVICE_NAME"] == _passed_name(app)
    attributes = dict(
        pair.split("=", 1) for pair in container["OTEL_RESOURCE_ATTRIBUTES"].split(",")
    )
    assert attributes["deployment.environment.name"] == "staging"
    assert attributes["service.name"] == _passed_name(app)
    assert attributes["infra.service.id"] == meta.service_id
    assert attributes["infra.iac.ref"] == release
    assert container["ENV"] == "staging", "the SDK resolves the tier from ENV"
    assert container["GIT_COMMIT_SHA"] == release, (
        "the SDK RuntimeIdentity reads the commit from GIT_COMMIT_SHA: 40 hex or unknown"
    )
    # export turns on with the endpoint: the vault-agent sidecar has no SDK and gets none
    vault_agent = container_env(app.parent / "compose.yaml", "vault-agent", pushed)
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in vault_agent
