"""Proof that infra2 consumes the released SDK rather than redefining shared contracts."""

from __future__ import annotations

import ast
import re
import subprocess
import textwrap
from pathlib import Path

import yaml

from common.tests import test_sdk_requirement as sdk_requirement_tests
from common.tests.test_sdk_requirement import _steps

ROOT = Path(__file__).resolve().parents[2]
OPS_CHECKS = ROOT / ".github/workflows/ops-checks.yml"


DEPRECATED_SDK_MODULES = {
    "images": "infra2_sdk.images",
    "deploy_health": "infra2_sdk.deploy_health (use infra2_sdk.deploy)",
    "dispatch": "infra2_sdk.dispatch (use infra2_sdk.deploy)",
    "release": "infra2_sdk.release (use infra2_sdk.refs)",
}

DEPRECATED_RUNTIME_MODULES = {
    "environ": "infra2_sdk.runtime.environ (use infra2_sdk.runtime.environment)",
    "dependencies": "infra2_sdk.runtime.dependencies (use infra2_sdk.runtime.health)",
    "probes": "infra2_sdk.runtime.probes (use infra2_sdk.runtime.health)",
}

DEPRECATED_SDK_ATTRS: dict[tuple[str, str], str] = {
    ("infra2_sdk.ci", "inspect_compose"): "infra2_sdk.rules.compose.inspect_compose",
    ("infra2_sdk.deploy", "identifier_kind"): "infra2_sdk.refs.identifier_kind",
    (
        "infra2_sdk.manifests",
        "assert_environment",
    ): "infra2_sdk.runtime.config_schema.assert_environment",
    ("infra2_sdk.observability", "ScheduleError"): "infra2_sdk.ci.ScheduleError",
    ("infra2_sdk.observability", "crons_in"): "infra2_sdk.ci.crons_in",
    ("infra2_sdk.observability", "largest_gap"): "infra2_sdk.ci.largest_gap",
}


def _module_violations(
    module: str, names: tuple[str, ...] = (), *, chain: bool = False
) -> list[str]:
    """Violations of one dotted ``infra2_sdk`` path.

    ``names`` are the names an ``import`` takes from the module. ``chain`` marks an
    attribute chain such as ``infra2_sdk.release.run``, where a dunder part like
    ``__version__`` is a public name.
    """
    parts = module.split(".")
    if parts[0] != "infra2_sdk":
        return []
    violations: list[str] = []
    for index, part in enumerate(parts[1:], start=1):
        dunder = part.startswith("__") and part.endswith("__")
        if part.startswith("_") and not (chain and dunder):
            violations.append(f"private module {'.'.join(parts[: index + 1])}")
            break
    for shim_name, shim_desc in DEPRECATED_SDK_MODULES.items():
        if parts[1:2] == [shim_name] or (len(parts) == 1 and shim_name in names):
            violations.append(f"deprecated module {shim_desc}")
    if len(parts) >= 2 and parts[1] == "runtime":
        for shim_name, shim_desc in DEPRECATED_RUNTIME_MODULES.items():
            if parts[2:3] == [shim_name] or (len(parts) == 2 and shim_name in names):
                violations.append(f"deprecated module {shim_desc}")
    for (attr_mod, attr_name), canonical in DEPRECATED_SDK_ATTRS.items():
        if module == attr_mod and attr_name in names:
            violations.append(
                f"deprecated attribute {attr_mod}.{attr_name} (use {canonical})"
            )
        elif chain and (
            module == f"{attr_mod}.{attr_name}"
            or module.startswith(f"{attr_mod}.{attr_name}.")
        ):
            violations.append(
                f"deprecated attribute {attr_mod}.{attr_name} (use {canonical})"
            )
    return violations


def _dotted(node: ast.expr) -> str | None:
    """``a.b.c`` for a chain of names and attributes, else ``None``."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


#: Calls that load a module from the string they get.
_LOADS_BY_NAME = frozenset({"import_module", "__import__", "find_spec"})


def _sdk_contract_violations(source: str) -> list[str]:
    """Use of an ``infra2_sdk`` private name or deprecated surface in ``source``.

    Only the published contract is stable across SDK releases (#955): a module or name
    starting with an underscore is private, ``infra2_sdk.images`` /
    ``to_otel_resource_attributes`` are removed in 3.0.0, and secondary module
    compatibility shims (deploy_health, dispatch, release, runtime.probes, etc.)
    are deprecated in favor of canonical modules (#1136).

    Three forms reach a module: an import, an attribute chain from the package or from
    an alias of it, and a call that loads a module by its name.
    """
    tree = ast.parse(source)
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname and alias.name.split(".")[0] == "infra2_sdk":
                    aliases[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.split(".")[0] == "infra2_sdk":
                for alias in node.names:
                    aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                violations.extend(_module_violations(alias.name))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = tuple(alias.name for alias in node.names)
            violations.extend(_module_violations(node.module, names))
            if node.module.split(".")[0] == "infra2_sdk":
                violations.extend(
                    f"private name {node.module}.{name}"
                    for name in names
                    if name.startswith("_")
                )
        elif isinstance(node, ast.Attribute):
            if node.attr == "to_otel_resource_attributes":
                violations.append(f"deprecated attribute {node.attr}")
            path = _dotted(node)
            if path:
                root, _, rest = path.partition(".")
                violations.extend(
                    _module_violations(f"{aliases.get(root, root)}.{rest}", chain=True)
                )
        elif isinstance(node, ast.Call):
            func = node.func
            loader = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if loader in _LOADS_BY_NAME:
                argument = node.args[0] if node.args else None
                for keyword in node.keywords:
                    if keyword.arg == "name":
                        argument = keyword.value
                if isinstance(argument, ast.Constant) and isinstance(
                    argument.value, str
                ):
                    violations.extend(_module_violations(argument.value))
    return list(dict.fromkeys(violations))


def _tracked_sources(pattern: str) -> list[Path]:
    """Tracked files that match ``pattern``, without the ``repos/`` checkouts."""
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--", pattern],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split("\0")
    return [
        ROOT / name
        for name in tracked
        if name and not name.startswith("repos/") and (ROOT / name).is_file()
    ]


#: ``python - <<'TAG'`` and ``python <<TAG``: the heredoc body is the program.
_PY_HEREDOC = re.compile(
    r"\bpython3?(?:[ \t]+-[A-Za-z]+)*(?:[ \t]+-)?[ \t]*<<-?[ \t]*"
    r"(?P<quote>['\"]?)(?P<tag>\w+)(?P=quote)[^\n]*\n"
    r"(?P<body>.*?)^[ \t]*(?P=tag)[ \t]*$",
    re.DOTALL | re.MULTILINE,
)
#: ``python3 -c '...'`` and ``python3 -c "..."``: the quoted string is the program.
_PY_DASH_C = re.compile(
    r"\bpython3?(?:[ \t]+-[A-Za-z]+)*[ \t]+-c[ \t]+(?P<quote>['\"])"
)
#: A shell expansion inside a double-quoted program: ``$HOME``, ``${HOME}``.
_SHELL_EXPANSION = re.compile(r"\$\{[^}]*\}|\$\w+")


def _shell_string(text: str, start: int, quote: str) -> str | None:
    """The text from ``start`` to the closing ``quote``, with shell escapes undone."""
    out: list[str] = []
    index = start
    while index < len(text):
        char = text[index]
        if quote == '"' and char == "\\" and index + 1 < len(text):
            following = text[index + 1]
            out.append(following if following in '"\\$`' else char + following)
            index += 2
            continue
        if char == quote:
            return "".join(out)
        out.append(char)
        index += 1
    return None


def _embedded_python(text: str) -> list[str]:
    """The Python programs that shell ``text`` runs through a heredoc or ``-c``."""
    found: list[tuple[int, str]] = []
    for match in _PY_HEREDOC.finditer(text):
        found.append((match.start(), textwrap.dedent(match.group("body"))))
    for match in _PY_DASH_C.finditer(text):
        program = _shell_string(text, match.end(), match.group("quote"))
        if program is not None:
            found.append((match.start(), program))
    return [program for _, program in sorted(found)]


def _embedded_violations(snippet: str) -> list[str]:
    """Contract violations of one embedded program that names ``infra2_sdk``."""
    if "infra2_sdk" not in snippet:
        return []
    for candidate in (snippet, _SHELL_EXPANSION.sub("0", snippet)):
        try:
            return _sdk_contract_violations(candidate)
        except SyntaxError:
            continue
    return ["unparseable python that names infra2_sdk"]


def _scan_workflow_python() -> tuple[int, dict[str, list[str]]]:
    """(programs seen, violations by ``file run N``) over every ``run`` step."""
    seen = 0
    per_file: dict[str, int] = {}
    found: dict[str, list[str]] = {}
    for name, run in _steps(normalize=False):
        for snippet in _embedded_python(run):
            seen += 1
            per_file[name] = per_file.get(name, 0) + 1
            if violations := _embedded_violations(snippet):
                found[f".github/{name} run {per_file[name]}"] = violations
    return seen, found


def _scan_shell_python(paths: list[Path]) -> tuple[int, dict[str, list[str]]]:
    """(programs seen, violations by file) over the shell files in ``paths``."""
    seen = 0
    found: dict[str, list[str]] = {}
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        label = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
        for snippet in _embedded_python(text):
            seen += 1
            if violations := _embedded_violations(snippet):
                found.setdefault(label, []).extend(violations)
    return seen, found


def test_infra_imports_only_the_published_sdk_contract() -> None:
    sources = _tracked_sources("*.py")
    assert len(sources) > 100, "the tracked-file scan found almost nothing"
    assert any(p.is_relative_to(ROOT / "platform") for p in sources), (
        "platform/ not covered"
    )
    assert any(p.is_relative_to(ROOT / "bootstrap") for p in sources), (
        "bootstrap/ not covered"
    )
    found = {
        str(path.relative_to(ROOT)): violations
        for path in sources
        if (violations := _sdk_contract_violations(path.read_text(encoding="utf-8")))
    }
    assert found == {}, f"private or deprecated infra2_sdk use: {found}"


def test_the_sdk_contract_scan_flags_private_and_deprecated_use() -> None:
    assert _sdk_contract_violations(
        "from infra2_sdk.refs import _ls_remote_rows, classify_ref\n"
    ) == ["private name infra2_sdk.refs._ls_remote_rows"]
    assert _sdk_contract_violations("import infra2_sdk._transport\n") == [
        "private module infra2_sdk._transport"
    ]
    assert _sdk_contract_violations("from infra2_sdk._transport import send\n") == [
        "private module infra2_sdk._transport"
    ]
    assert _sdk_contract_violations("from infra2_sdk import images\n") == [
        "deprecated module infra2_sdk.images"
    ]
    assert _sdk_contract_violations("import infra2_sdk.images\n") == [
        "deprecated module infra2_sdk.images"
    ]
    assert _sdk_contract_violations("identity.to_otel_resource_attributes()\n") == [
        "deprecated attribute to_otel_resource_attributes"
    ]
    assert _sdk_contract_violations(
        "from infra2_sdk.deploy_health import poll_until_healthy\n"
    ) == ["deprecated module infra2_sdk.deploy_health (use infra2_sdk.deploy)"]
    assert _sdk_contract_violations(
        "from infra2_sdk.release import ReleaseError\n"
    ) == ["deprecated module infra2_sdk.release (use infra2_sdk.refs)"]
    assert _sdk_contract_violations(
        "from infra2_sdk.runtime.probes import DependencyStatus\n"
    ) == ["deprecated module infra2_sdk.runtime.probes (use infra2_sdk.runtime.health)"]
    for (mod, attr), canonical in DEPRECATED_SDK_ATTRS.items():
        expected = f"deprecated attribute {mod}.{attr} (use {canonical})"
        assert _sdk_contract_violations(f"from {mod} import {attr}\n") == [expected]
        assert _sdk_contract_violations(f"import {mod}\n{mod}.{attr}\n") == [expected]
    assert (
        _sdk_contract_violations(
            "from infra2_sdk.refs import ls_remote_rows, redact_repo\n"
            "from other_pkg._private import _x\n"
        )
        == []
    )


_RELEASE = "deprecated module infra2_sdk.release (use infra2_sdk.refs)"
_DISPATCH = "deprecated module infra2_sdk.dispatch (use infra2_sdk.deploy)"
_DEPLOY_HEALTH = "deprecated module infra2_sdk.deploy_health (use infra2_sdk.deploy)"
_IMAGES = "deprecated module infra2_sdk.images"
_TRANSPORT = "private module infra2_sdk._transport"
_RUNTIME_PROBES = (
    "deprecated module infra2_sdk.runtime.probes (use infra2_sdk.runtime.health)"
)
_RUNTIME_ENVIRON = (
    "deprecated module infra2_sdk.runtime.environ (use infra2_sdk.runtime.environment)"
)


def test_the_sdk_contract_scan_flags_attribute_chains() -> None:
    """Code reaches a removed module through the package as well as through an import."""
    reached_through_the_package = {
        "infra2_sdk.release.run()": _RELEASE,
        "infra2_sdk.dispatch.send()": _DISPATCH,
        "value = infra2_sdk.deploy_health.poll": _DEPLOY_HEALTH,
        "infra2_sdk.images": _IMAGES,
        "infra2_sdk._transport.send()": _TRANSPORT,
        "infra2_sdk.runtime.probes.check()": _RUNTIME_PROBES,
    }
    for statement, expected in reached_through_the_package.items():
        source = f"import infra2_sdk\n{statement}\n"
        assert _sdk_contract_violations(source) == [expected], statement
    # An alias of the package, or of a sub-package, leads to the same module.
    assert _sdk_contract_violations(
        "import infra2_sdk as sdk\nsdk.release.run()\n"
    ) == [_RELEASE]
    assert _sdk_contract_violations(
        "from infra2_sdk import runtime as rt\nrt.probes.check()\n"
    ) == [_RUNTIME_PROBES]
    assert _sdk_contract_violations(
        "from infra2_sdk import runtime\nruntime.environ.get()\n"
    ) == [_RUNTIME_ENVIRON]
    # Published names, dunder names and chains of other packages stay clean.
    assert (
        _sdk_contract_violations(
            "import infra2_sdk\n"
            "print(infra2_sdk.__version__, infra2_sdk.refs.classify_ref)\n"
            "import other\n"
            "other.release.run()\n"
            "other.images\n"
        )
        == []
    )


def test_the_sdk_contract_scan_flags_modules_loaded_by_name() -> None:
    loaded_by_name = {
        'import importlib\nimportlib.import_module("infra2_sdk.release")\n': _RELEASE,
        'from importlib import import_module\nimport_module("infra2_sdk.dispatch")\n': _DISPATCH,
        '__import__("infra2_sdk.images")\n': _IMAGES,
        'import importlib\nimportlib.import_module("infra2_sdk._transport")\n': _TRANSPORT,
        'import importlib\nimportlib.import_module(name="infra2_sdk.deploy_health")\n': _DEPLOY_HEALTH,
    }
    for source, expected in loaded_by_name.items():
        assert _sdk_contract_violations(source) == [expected], source
    assert (
        _sdk_contract_violations(
            "import importlib\n"
            'importlib.import_module("infra2_sdk.refs")\n'
            'importlib.import_module("other.release")\n'
        )
        == []
    )


#: Shell text in the three shapes the repository uses. The first block is not Python.
_HEREDOC = """\
cat <<EOF
from infra2_sdk.release import not_python
EOF
docker exec -i "$CONTAINER" python - <<'PY'
import infra2_sdk
infra2_sdk._transport.send()
PY
"""
#: The shape of the deleted tools/sync_anonymized_staging.sh: a double-quoted `-c`.
_DASH_C_DOUBLE = r"""
manifest="$(python3 -c "
from infra2_sdk.release import ReleaseError
print(\"ok: $HOME\")
")"
echo done
"""
_DASH_C_SINGLE = "python -c 'import infra2_sdk.dispatch; print(1)'"


def test_embedded_python_is_cut_out_of_shell_text() -> None:
    assert _embedded_python(_HEREDOC) == [
        "import infra2_sdk\ninfra2_sdk._transport.send()\n"
    ]
    (double,) = _embedded_python(_DASH_C_DOUBLE)
    assert double.strip().splitlines() == [
        "from infra2_sdk.release import ReleaseError",
        'print("ok: $HOME")',
    ]
    assert _embedded_python(_DASH_C_SINGLE) == ["import infra2_sdk.dispatch; print(1)"]
    assert _embedded_python("echo python is fine\nsort <<EOF\nb\nEOF\n") == []


def test_embedded_python_with_removed_sdk_use_is_flagged() -> None:
    assert _embedded_violations("from infra2_sdk.release import ReleaseError\n") == [
        _RELEASE
    ]
    assert _embedded_violations("import json\nprint(json.dumps(1))\n") == []
    # A shell expansion inside a double-quoted `-c` string does not hide the import.
    assert _embedded_violations("from infra2_sdk.release import X\nprint($HOME)\n") == [
        _RELEASE
    ]
    # Code that names the SDK and cannot be parsed fails closed.
    assert _embedded_violations("import infra2_sdk\nthis is ( not python\n") == [
        "unparseable python that names infra2_sdk"
    ]


def test_python_in_workflow_run_blocks_is_scanned(tmp_path, monkeypatch) -> None:
    workflow = tmp_path / "workflows" / "demo.yml"
    workflow.parent.mkdir()
    workflow.write_text(
        "name: demo\n"
        "on: push\n"
        "jobs:\n"
        "  demo:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: |\n"
        "          python - <<'PY'\n"
        "          import infra2_sdk\n"
        "          infra2_sdk.release.run()\n"
        "          PY\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sdk_requirement_tests, "GITHUB", tmp_path)
    label = f".github/{workflow.relative_to(tmp_path).as_posix()} run 1"
    assert _scan_workflow_python() == (1, {label: [_RELEASE]})


def test_python_in_shell_files_is_scanned(tmp_path) -> None:
    script = tmp_path / "wait.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "docker exec -i \"$C\" python - <<'PY'\n"
        "from infra2_sdk.dispatch import send\n"
        "PY\n",
        encoding="utf-8",
    )
    assert _scan_shell_python([script]) == (1, {str(script): [_DISPATCH]})


def test_python_embedded_in_workflows_and_shell_uses_only_the_published_contract() -> (
    None
):
    workflow_blocks, workflow_found = _scan_workflow_python()
    shell_blocks, shell_found = _scan_shell_python(_tracked_sources("*.sh"))
    # ops-checks.yml alone holds a dozen heredocs. wait_for_idle.sh holds one heredoc and
    # host_backup.sh holds one `-c` program. A scan that finds fewer reads nothing, and
    # the check below would pass on an empty set.
    assert workflow_blocks >= 12, f"the scan found {workflow_blocks} workflow blocks"
    assert shell_blocks >= 2, f"the scan found {shell_blocks} shell blocks"
    assert {**workflow_found, **shell_found} == {}


def test_deploy_canary_installs_the_declared_sdk_requirement() -> None:
    workflow = yaml.safe_load(OPS_CHECKS.read_text(encoding="utf-8"))
    job = workflow["jobs"]["deploy-v2-canary"]
    steps = {step["name"]: step for step in job["steps"]}
    install_command = steps["Install runtime dependencies"]["run"]

    assert 'sdk_requirement="$(python tools/sdk_requirement.py)"' in install_command
    assert (
        'python -m pip install --no-deps "$sdk_requirement"' in install_command
        and "python -m pip install -e ." in install_command
    )
    for event in ("push", "pull_request"):
        paths = workflow["on"][event]["paths"]
        # The installed SDK is the wheel uv.lock pins, read by tools/sdk_requirement.py
        # (#1115). Moving the repos/infra2-sdk submodule pointer never changes it, so
        # that path was a no-op trigger and was removed (#506).
        assert {"pyproject.toml", "uv.lock", "tools/sdk_requirement.py"} <= set(paths)
        assert "repos/infra2-sdk" not in paths


def test_reserved_deploy_canary_slot_is_globally_serialized() -> None:
    workflow = yaml.safe_load(OPS_CHECKS.read_text(encoding="utf-8"))
    concurrency = workflow["jobs"]["deploy-v2-canary"]["concurrency"]

    assert concurrency == {
        "group": "deploy-v2-canary",
        "cancel-in-progress": False,
    }


def test_pull_request_canary_binds_exact_head_authority_to_cloneable_branch() -> None:
    workflow = yaml.safe_load(OPS_CHECKS.read_text(encoding="utf-8"))
    job = workflow["jobs"]["deploy-v2-canary"]
    steps = {step["name"]: step for step in job["steps"]}
    env = steps["Run deploy_v2 canary"]["env"]
    iac_ref = env["IAC_REF_INPUT"]
    clone_ref = env["IAC_CLONE_REF_INPUT"]

    assert "github.event_name == 'pull_request'" in iac_ref
    assert "github.event.pull_request.head.sha" in iac_ref
    assert iac_ref.endswith("|| 'main' }}")
    assert "github.head_ref" in clone_ref
    assert clone_ref.endswith("|| '' }}")

    run_command = steps["Run deploy_v2 canary"]["run"]
    assert 'clone_args+=(--iac-clone-ref "$IAC_CLONE_REF_INPUT")' in run_command
    assert '"${clone_args[@]}"' in run_command


def test_retired_compatibility_modules_stay_removed() -> None:
    assert not (ROOT / "common/ci_gate_schema.py").exists()
    assert not (ROOT / "common/pipeline_stage_contract.py").exists()
    assert not (ROOT / "common/image_pins.py").exists()
    assert not (ROOT / "tools/lint_platform_image_pins.py").exists()
