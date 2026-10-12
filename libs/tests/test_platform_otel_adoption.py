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
from libs.tests.ambient_env import clear_telemetry_env
from libs.tests.compose_env import compose_services, container_env, raw_environment

ROOT = Path(__file__).resolve().parents[2]
PLATFORM = ROOT / "platform"
COLLECTOR_OTLP_HTTP = "http://platform-signoz-otel-collector:4318"
#: The callers that exist today. They prove that the scanner finds something.
KNOWN_CALLERS = {"platform/12.alerting/app.py", "platform/30.todo/app.py"}


def _last_name(node: ast.expr) -> str:
    """`boot` for `boot`, `init_service` for `sdk.init_service`, else an empty string."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _names_bound_to(tree: ast.AST, names: set[str]) -> set[str]:
    """Every local name that stands for one of `names`.

    That is the names themselves, the `as` name of an import, and every name assigned from
    one of them (`boot = init_service`, `run = boot`, `boot = sdk.init_service`).
    """
    bound = set(names)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            bound |= {a.asname for a in node.names if a.name in names and a.asname}
    grown = True
    while grown:
        grown = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and _last_name(node.value) in bound:
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id not in bound:
                        bound.add(target.id)
                        grown = True
    return bound


def _is_call_to(call: ast.Call, names: set[str], bound: set[str]) -> bool:
    if _last_name(call.func) in bound:
        return True
    # getattr(module, "init_service")(...)
    inner = call.func
    return (
        isinstance(inner, ast.Call)
        and _last_name(inner.func) == "getattr"
        and len(inner.args) >= 2
        and isinstance(inner.args[1], ast.Constant)
        and inner.args[1].value in names
    )


def calls_named(source: str, names: set[str]) -> list[ast.Call]:
    """The calls in `source` to one of `names`, under any name the source gives them.

    A comment or a string is not a call. An alias is: `from x import init_service as boot`
    followed by `boot(...)` is a call to `init_service`.
    """
    tree = ast.parse(source)
    bound = _names_bound_to(tree, names)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_call_to(node, names, bound)
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


@pytest.mark.parametrize(
    "source",
    [
        "from infra2_sdk.fastapi import init_service as boot\nboot(app, name='a')",
        "from infra2_sdk.fastapi import init_service\nboot = init_service\n"
        "run = boot\nrun(app, name='a')",
        "import infra2_sdk.fastapi as sdk\nsdk.init_service(app, name='a')",
        "import infra2_sdk.fastapi as sdk\nboot = sdk.init_service\nboot(app, name='a')",
        "import infra2_sdk.fastapi as sdk\ngetattr(sdk, 'init_service')(app, name='a')",
        "def start():\n    from infra2_sdk.fastapi import init_service as boot\n"
        "    boot(app, name='a')",
    ],
    ids=[
        "from-import-alias",
        "assigned-alias",
        "module-alias",
        "attribute-alias",
        "getattr",
        "alias-in-function",
    ],
)
def test_the_scanner_follows_every_name_of_init_service(source: str) -> None:
    """An aliased call is still a call. A scanner that misses it passes a service whose
    deploy, compose and image links nothing checks."""
    assert len(init_service_calls(source)) == 1


@pytest.mark.parametrize(
    "source",
    [
        "from infra2_sdk.fastapi import init_service as boot",
        "from elsewhere import other as boot\nboot(app)",
        "boot = print\nboot(app)",
        "import infra2_sdk.fastapi as sdk\nx = sdk.init_service",
    ],
    ids=["alias-not-called", "other-function", "other-assignment", "not-called"],
)
def test_the_scanner_ignores_a_name_that_does_not_call_init_service(
    source: str,
) -> None:
    assert init_service_calls(source) == []


def test_the_second_bootstrap_scan_follows_an_alias_too() -> None:
    source = (
        "from infra2_sdk.runtime.otel import configure_telemetry as start\n"
        "start(settings, set_global=True)"
    )
    assert len(calls_named(source, OWN_BOOTSTRAPS)) == 1


def test_the_scan_finds_at_least_one_caller() -> None:
    """An empty list would run every parametrized test zero times and still pass."""
    assert CALLERS


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


@pytest.mark.parametrize("app", CALLERS, ids=CALLER_IDS)
def test_the_app_starts_telemetry_when_the_deploy_issued_the_endpoint(
    app: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the issued identity and a valid endpoint, loading the app starts the one
    bootstrap and keeps its providers. `enable_otel=False` would leave both out."""
    import infra2_sdk.runtime.otel as sdk_otel

    from libs.core.service_identity import ServiceIdentity

    started: list[object] = []

    class _Providers:
        def shutdown(self):
            return None

    def fake_configure(settings, **kwargs):
        started.append(settings)
        return _Providers()

    monkeypatch.setattr(sdk_otel, "configure_telemetry", fake_configure)
    clear_telemetry_env(monkeypatch)
    meta = _meta(app)
    identity = ServiceIdentity.build(
        meta.service_id,
        "staging",
        component=meta.service,
        service_name=_passed_name(app),
        version="a" * 40,
        iac_ref="a" * 40,
    )
    issued = {
        "ENV": "staging",
        "OTEL_EXPORTER_OTLP_ENDPOINT": COLLECTOR_OTLP_HTTP,
        "OTEL_SERVICE_NAME": identity.service_name,
        "OTEL_RESOURCE_ATTRIBUTES": identity.otel_resource_attributes(),
    }
    for key, value in issued.items():
        monkeypatch.setenv(key, value)

    module = _load(f"adoption_app_{app.parent.name.replace('.', '_')}", app)

    (settings,) = started
    assert settings.service_name == _passed_name(app)
    assert settings.endpoint == COLLECTOR_OTLP_HTTP
    assert module.fastapi_app.state.telemetry_providers is not None


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
