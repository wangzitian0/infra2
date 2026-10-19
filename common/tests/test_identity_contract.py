"""Identity contract for `GIT_COMMIT_SHA` and `ServiceIdentity` (#1211).

The contract is the table in `docs/ssot/core.environments.md`, section 4.5.1. It names two
service classes. A `platform` service gets the full infra2 SHA of the deploy. An `app`
gets the image tag. These tests parse the table. They compare it with the registry, the
compose files, and the deploy code.

These tests state three facts.

1. Each compose file that a registered service with telemetry ships sets `GIT_COMMIT_SHA`
   to the value of its class.
2. No non-test code uses `RuntimeIdentity`.
3. The three deploy entry points render `OTEL_RESOURCE_ATTRIBUTES` from `ServiceIdentity`.

A guard that never fails proves nothing. Each guard here has mutation tests: a seeded
wrong state must produce a violation.
"""

from __future__ import annotations

import ast
import copy
import re
from pathlib import Path

import pytest

from common.core import registry as reg
from common.core.service_identity import ServiceIdentity
from common.deploy.env_config import preview_service_config
from common.tests.compose_env import compose_services, raw_environment, resolve
from common.tests.test_sdk_contract_adoption import _tracked_sources

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_DOC = ROOT / "docs/ssot/core.environments.md"
CONTRACT_ANCHOR = '<a id="commit-identity"></a>'
CONTRACT_COLUMNS = [
    "Class",
    "Services",
    "Compose value",
    "Value in the container",
    "Issued by",
    "Reader",
]
ENV_KEY = "GIT_COMMIT_SHA"

#: The service class that the contract assigns to each registry layer. A new layer raises
#: `KeyError`: the author must decide its class and update the contract table.
CLASS_OF_LAYER = {"platform": "platform", "finance_report": "app", "truealpha": "app"}

SHA40 = "1af32e6daf17e2c58383dd2c0bfaea13bc11e517"
SHORT_SHA = SHA40[:7]
TAG = "v0.1.74"
#: The container value of each class when the deploy issues the values below.
EXPECTED_VALUE = {"platform": SHA40, "app": TAG}


# --- the contract table -----------------------------------------------------------------


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _ticks(cell: str) -> list[str]:
    """The text of every backtick span in a table cell."""
    return re.findall(r"`([^`]+)`", cell)


def contract_table() -> dict[str, dict[str, str]]:
    """The contract rows by service class, parsed from the Markdown table."""
    lines = CONTRACT_DOC.read_text(encoding="utf-8").splitlines()
    assert lines.count(CONTRACT_ANCHOR) == 1, "the contract anchor must exist once"
    table: list[str] = []
    for line in lines[lines.index(CONTRACT_ANCHOR) + 1 :]:
        if line.startswith("|"):
            table.append(line)
        elif table:
            break
    header, _separator, *body = (_cells(line) for line in table)
    assert header == CONTRACT_COLUMNS
    rows: dict[str, dict[str, str]] = {}
    for cells in body:
        row = dict(zip(header, cells, strict=True))
        (service_class,) = _ticks(row["Class"])
        assert service_class not in rows, f"duplicate row for class {service_class}"
        rows[service_class] = row
    return rows


# --- the services under contract --------------------------------------------------------


def telemetry_services() -> dict[str, reg.ServiceMeta]:
    """The registered services whose Deployer sets `telemetry_service_name`."""
    return {
        service_id: meta
        for service_id, meta in reg.service_attrs().items()
        if meta.telemetry_service_name
    }


def class_of(meta: reg.ServiceMeta) -> str:
    return CLASS_OF_LAYER[meta.layer]


def issued_env(meta: reg.ServiceMeta) -> dict[str, str]:
    """What the deploy puts in the compose env of this service.

    `Deployer.sync` (`common/deploy/sync_pipeline.py`) issues the `INFRA_*` values only.
    `promote` and `preview` also issue the image tag as `IMAGE_TAG` and `GIT_COMMIT_SHA`.
    """
    if class_of(meta) == "platform":
        identity = ServiceIdentity.build(
            meta.service_id, "production", version=SHA40, iac_ref=SHA40
        )
        return identity.deploy_env()
    identity = ServiceIdentity.build(
        meta.service_id, "production", version=TAG, iac_ref=SHA40
    )
    return {**identity.deploy_env(), "IMAGE_TAG": TAG, ENV_KEY: TAG}


def commit_env_violations(
    services: dict[str, dict],
    *,
    service_class: str,
    issued: dict[str, str],
    must_set: str | None = None,
) -> list[str]:
    """Violations of the contract in the compose `services` of one registered service.

    Every compose service that sets `GIT_COMMIT_SHA` must receive the value of its class
    when the deploy issues `issued`. It must receive `unknown` when the deploy issues
    nothing. The service `must_set` has to set the key.
    """
    expected = EXPECTED_VALUE[service_class]
    setters = {
        name: raw_environment(service)[ENV_KEY]
        for name, service in services.items()
        if ENV_KEY in raw_environment(service)
    }
    violations = []
    if not setters:
        violations.append(f"no compose service sets {ENV_KEY}")
    if must_set and must_set not in setters:
        violations.append(f"{must_set} does not set {ENV_KEY}")
    for name, raw in setters.items():
        got = resolve(raw, issued)
        if got != expected:
            violations.append(f"{name}: {raw!r} gives {got!r}, expected {expected!r}")
        if resolve(raw, {}) != "unknown":
            violations.append(
                f"{name}: {raw!r} must give 'unknown' when nothing is issued"
            )
    return violations


def violations_of(meta: reg.ServiceMeta, services: dict[str, dict]) -> list[str]:
    service_class = class_of(meta)
    return commit_env_violations(
        services,
        service_class=service_class,
        issued=issued_env(meta),
        # The telemetry service of a platform compose is the compose service of the
        # Deployer. An app Deployer names its Traefik service, which can differ.
        must_set=meta.service_name if service_class == "platform" else None,
    )


def compose_of(meta: reg.ServiceMeta) -> dict[str, dict]:
    return compose_services(ROOT / meta.compose_path)


def commit_values(meta: reg.ServiceMeta) -> dict[str, str]:
    """The `GIT_COMMIT_SHA` value in each container of this service after the deploy."""
    return {
        name: resolve(raw, issued_env(meta))
        for name, service in compose_of(meta).items()
        if (raw := raw_environment(service).get(ENV_KEY)) is not None
    }


SERVICE_IDS = sorted(telemetry_services())


@pytest.mark.parametrize("service_id", SERVICE_IDS)
def test_compose_sets_the_commit_of_its_class(service_id: str) -> None:
    meta = telemetry_services()[service_id]
    assert violations_of(meta, compose_of(meta)) == []


def test_guard_covers_both_classes_and_all_four_services() -> None:
    """The parametrization is not empty and reaches both classes."""
    services = telemetry_services()
    assert {class_of(meta) for meta in services.values()} == {"platform", "app"}
    assert set(services) == {
        "platform/alerting",
        "platform/todo",
        "finance_report/app",
        "truealpha/app",
        "truealpha/data_engine",
    }


def _mutated(meta: reg.ServiceMeta, compose_service: str, value: str | None) -> dict:
    services = copy.deepcopy(compose_of(meta))
    environment = raw_environment(services[compose_service])
    if value is None:
        del environment[ENV_KEY]
    else:
        environment[ENV_KEY] = value
    services[compose_service]["environment"] = environment
    return services


@pytest.mark.parametrize(
    ("service_id", "compose_service", "mutant"),
    [
        ("platform/todo", "todo", "unknown"),
        ("platform/todo", "todo", None),
        ("platform/todo", "todo", "${GIT_COMMIT_SHA:-unknown}"),
        ("platform/todo", "todo", "${INFRA_IAC_REF}"),
        ("platform/todo", "todo", SHA40),
        ("platform/alerting", "feishu-alert-bridge", "unknown"),
        ("platform/alerting", "feishu-alert-bridge", None),
        ("finance_report/app", "backend", "unknown"),
        ("finance_report/app", "backend", "${INFRA_IAC_REF:-unknown}"),
        ("truealpha/app", "web", "${INFRA_IAC_REF:-unknown}"),
        ("truealpha/app", "llm", "unknown"),
    ],
    ids=lambda value: str(value).replace("/", "_"),
)
def test_guard_rejects_a_seeded_wrong_compose_value(
    service_id: str, compose_service: str, mutant: str | None
) -> None:
    """RED proof: a compose file with the wrong value gives at least one violation."""
    meta = telemetry_services()[service_id]
    assert violations_of(meta, compose_of(meta)) == [], (
        "the unmutated file is the control"
    )
    assert violations_of(meta, _mutated(meta, compose_service, mutant)) != []


def test_guard_rejects_a_platform_compose_that_drops_the_key_everywhere() -> None:
    meta = telemetry_services()["platform/todo"]
    services = copy.deepcopy(compose_of(meta))
    for service in services.values():
        environment = raw_environment(service)
        environment.pop(ENV_KEY, None)
        service["environment"] = environment
    assert violations_of(meta, services) == [
        f"no compose service sets {ENV_KEY}",
        f"todo does not set {ENV_KEY}",
    ]


# --- the document names what the guard checks -------------------------------------------


def test_contract_names_the_service_classes_the_guard_checks() -> None:
    assert set(contract_table()) == set(CLASS_OF_LAYER.values())


def test_contract_names_the_services_of_each_class() -> None:
    for service_class, row in contract_table().items():
        expected = {
            service_id
            for service_id, meta in telemetry_services().items()
            if class_of(meta) == service_class
        }
        assert expected, f"the guard checks no service of class {service_class}"
        assert set(_ticks(row["Services"])) == expected, service_class


def test_contract_compose_value_equals_the_compose_files() -> None:
    table = contract_table()
    checked = 0
    for meta in telemetry_services().values():
        documented = _ticks(table[class_of(meta)]["Compose value"])
        for service in compose_of(meta).values():
            raw = raw_environment(service).get(ENV_KEY)
            if raw is not None:
                assert documented == [raw], meta.service_id
                checked += 1
    assert checked == len(PLATFORM_VALUES) + len(APP_VALUES) > 0


def _read_env_vars(source: str) -> set[str]:
    """Names that `source` reads from the process environment by a literal key."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            chain = ast.unparse(node.func)
            if chain in {"os.environ.get", "os.getenv", "os.environ.pop"}:
                names.add(str(node.args[0].value))
        if (
            isinstance(node, ast.Subscript)
            and ast.unparse(node.value) == "os.environ"
            and isinstance(node.slice, ast.Constant)
        ):
            names.add(str(node.slice.value))
    return names


def test_read_env_vars_finds_the_forms_it_claims_to_find() -> None:
    source = (
        "import os\n"
        "a = os.environ.get('A', 'x')\nb = os.getenv('B')\nc = os.environ['C']\n"
        "d = other.get('D')\ne = 'E'\n"
    )
    assert _read_env_vars(source) == {"A", "B", "C"}


def test_only_the_todo_page_reads_the_commit_from_the_environment() -> None:
    readers = sorted(
        path.relative_to(ROOT).as_posix()
        for path in _non_test_python()
        if ENV_KEY in _read_env_vars(path.read_text(encoding="utf-8"))
    )
    assert readers == ["platform/30.todo/app.py"]
    platform_row = contract_table()["platform"]
    documented = [
        tick for tick in _ticks(platform_row["Reader"]) if tick.endswith(".py")
    ]
    assert documented == readers


def test_contract_code_anchors_exist() -> None:
    for service_class, row in contract_table().items():
        for column in ("Issued by", "Reader"):
            anchors = [tick for tick in _ticks(row[column]) if tick.endswith(".py")]
            assert anchors, f"{service_class}/{column} names no code file"
            for anchor in anchors:
                assert (ROOT / anchor).is_file(), f"{service_class}/{column}: {anchor}"


def test_preview_readiness_probe_reads_the_app_health_version_fields() -> None:
    """The `app` reader: the finance_report preview probe compares `git_sha`/`version`."""
    probes = preview_service_config("finance_report/app").readiness_probes
    health = [probe for probe in probes if probe.path == "/api/health"]
    assert [probe.version_fields for probe in health] == [("git_sha", "version")]


# --- RuntimeIdentity has no user in infra2 ----------------------------------------------


def _in_modules(dotted: str, modules: frozenset[str]) -> bool:
    return any(
        dotted == module or dotted.startswith(module + ".") for module in modules
    )


def sdk_uses(
    source: str, *, names: frozenset[str], modules: frozenset[str]
) -> list[int]:
    """Line numbers where `source` imports a module of `modules` or names one of `names`.

    A module in `modules` matches itself and its submodules. A name matches an import
    alias, a variable, an attribute and a string that holds the name or a dotted module
    path. A comment, a docstring with more text, and a relative import do not match.
    """
    hits: list[int] = []
    for node in ast.walk(ast.parse(source)):
        hit = False
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            module = node.module or ""
            hit = any(
                alias.name in names or _in_modules(f"{module}.{alias.name}", modules)
                for alias in node.names
            )
        elif isinstance(node, ast.Import):
            hit = any(_in_modules(alias.name, modules) for alias in node.names)
        elif isinstance(node, ast.Name):
            hit = node.id in names
        elif isinstance(node, ast.Attribute):
            hit = node.attr in names
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            hit = node.value in names or (
                re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", node.value) is not None
                and _in_modules(node.value, modules)
            )
        if hit:
            hits.append(node.lineno)
    return hits


def _non_test_python() -> list[Path]:
    """Tracked Python files outside `repos/` and outside every `tests` directory."""
    return [
        path
        for path in _tracked_sources("*.py")
        if "tests" not in path.relative_to(ROOT).parts and path.name != "conftest.py"
    ]


def scan(*, names: frozenset[str], modules: frozenset[str]) -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in _non_test_python():
        lines = sdk_uses(path.read_text(encoding="utf-8"), names=names, modules=modules)
        if lines:
            found[path.relative_to(ROOT).as_posix()] = lines
    return found


RUNTIME_IDENTITY = {
    "names": frozenset({"RuntimeIdentity"}),
    "modules": frozenset({"infra2_sdk.runtime.identity"}),
}


@pytest.mark.parametrize(
    "source",
    [
        "from infra2_sdk.runtime.identity import RuntimeIdentity",
        "from infra2_sdk.runtime import RuntimeIdentity as Identity",
        "from infra2_sdk.runtime import identity",
        "from infra2_sdk.runtime.identity import *",
        "import infra2_sdk.runtime.identity as ident",
        "import infra2_sdk\nidentity = infra2_sdk.runtime.RuntimeIdentity.from_env()",
        "import importlib\nimportlib.import_module('infra2_sdk.runtime.identity')",
        "identity = getattr(runtime, 'RuntimeIdentity')",
    ],
)
def test_scanner_finds_each_way_to_use_runtime_identity(source: str) -> None:
    assert sdk_uses(source, **RUNTIME_IDENTITY) != []


@pytest.mark.parametrize(
    "source",
    [
        '"""This module does not use RuntimeIdentity."""',
        "# RuntimeIdentity is not used here\nx = 1",
        "from infra2_sdk.runtime import OtelSettings",
        "from infra2_sdk.fastapi import init_service",
        "from .identity import Helper",
        "message = 'RuntimeIdentity rejects a tag'",
    ],
)
def test_scanner_ignores_text_that_is_not_a_use(source: str) -> None:
    assert sdk_uses(source, **RUNTIME_IDENTITY) == []


def test_scanner_finds_the_infra2_sdk_imports_of_the_tree() -> None:
    """Positive control: the same scan over the same files finds the SDK users that exist."""
    files = _non_test_python()
    assert len(files) > 100, "the scan must reach the tracked tree"
    found = scan(names=frozenset(), modules=frozenset({"infra2_sdk"}))
    assert {
        "platform/12.alerting/app.py",
        "platform/30.todo/app.py",
        "common/deploy/refs.py",
        "common/deploy/preview.py",
    } <= set(found)
    assert len(found) >= 15


def test_runtime_identity_has_no_non_test_user() -> None:
    """The contract says infra2 does not adopt `RuntimeIdentity`.

    A service that adopts it must meet the conditions of section 4.5.1 first. The
    adopting pull request updates the contract table and this guard together.
    """
    assert scan(**RUNTIME_IDENTITY) == {}


# --- ServiceIdentity stays the source of the resource attributes ------------------------


def _is_const(node: ast.AST | None, value: str) -> bool:
    return isinstance(node, ast.Constant) and node.value == value


def otel_attribute_sites() -> dict[str, list[ast.expr]]:
    """The expressions that non-test code assigns to `OTEL_RESOURCE_ATTRIBUTES`."""
    sites: dict[str, list[ast.expr]] = {}
    for path in _non_test_python():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            values: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                values = [
                    node.value
                    for target in node.targets
                    if isinstance(target, ast.Subscript)
                    and _is_const(target.slice, "OTEL_RESOURCE_ATTRIBUTES")
                ]
            elif isinstance(node, ast.Dict):
                values = [
                    value
                    for key, value in zip(node.keys, node.values, strict=True)
                    if _is_const(key, "OTEL_RESOURCE_ATTRIBUTES")
                ]
            if values:
                sites.setdefault(path.relative_to(ROOT).as_posix(), []).extend(values)
    return sites


def _renders_from_service_identity(value: ast.expr) -> bool:
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "otel_resource_attributes"
    )


def test_every_issuing_site_renders_the_attributes_from_service_identity() -> None:
    sites = otel_attribute_sites()
    assert set(sites) == {
        "common/deploy/promote.py",
        "common/deploy/preview.py",
        "common/deploy/sync_pipeline.py",
    }
    for path, values in sites.items():
        assert all(_renders_from_service_identity(value) for value in values), path


def test_site_check_rejects_an_attribute_string_built_by_hand() -> None:
    hand_built = ast.parse("env['OTEL_RESOURCE_ATTRIBUTES'] = 'a=b'").body[0]
    assert isinstance(hand_built, ast.Assign)
    assert not _renders_from_service_identity(hand_built.value)
    rendered = ast.parse("x = identity.otel_resource_attributes()").body[0]
    assert isinstance(rendered, ast.Assign)
    assert _renders_from_service_identity(rendered.value)


def test_no_non_test_code_calls_the_sdk_attribute_emitter() -> None:
    emitter = frozenset({"to_standard_otel_resource_attributes"})
    assert scan(names=emitter, modules=frozenset()) == {}


def test_service_identity_emits_the_five_infra_resource_keys() -> None:
    identity = ServiceIdentity.build(
        "platform/todo", "production", version=SHA40, iac_ref=SHA40
    )
    ours = dict(
        pair.split("=", 1) for pair in identity.otel_resource_attributes().split(",")
    )
    infra_keys = {key for key in ours if key.startswith("infra.")}
    assert infra_keys == {
        "infra.component",
        "infra.iac.ref",
        "infra.identity.schema",
        "infra.managed_by",
        "infra.service.id",
    }


PLATFORM_VALUES = [
    (service_id, name, value)
    for service_id, meta in sorted(telemetry_services().items())
    if class_of(meta) == "platform"
    for name, value in commit_values(meta).items()
]
APP_VALUES = [
    (service_id, name, value)
    for service_id, meta in sorted(telemetry_services().items())
    if class_of(meta) == "app"
    for name, value in commit_values(meta).items()
]


def test_the_container_values_exist_for_both_classes() -> None:
    """Control: the container values exist for every service of both classes."""
    assert {(service_id, name) for service_id, name, _ in PLATFORM_VALUES} == {
        ("platform/alerting", "feishu-alert-bridge"),
        ("platform/todo", "todo"),
    }
    assert {service_id for service_id, _, _ in APP_VALUES} == {
        "finance_report/app",
        "truealpha/app",
        "truealpha/data_engine",
    }
    assert {value for _, _, value in PLATFORM_VALUES} == {SHA40}
    assert {value for _, _, value in APP_VALUES} == {TAG}
