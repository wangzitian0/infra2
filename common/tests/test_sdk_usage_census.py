"""Fixture, mutation, and repository contract tests for tools/sdk_usage_census.py (#1246)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest

from common.core import sdk_usage_census as census

PKG_HYPHEN = "infra2" + "-sdk"
PKG_UNDER = "infra2" + "_sdk"
VER_LATEST = "9.8.7"
VER_OLD = "9.6.5"


def _write(root: Path, rel: str, content: str) -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    return target


def _write_files(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        _write(root, rel, content)


def _run_and_eval(
    workspace: tuple[Path, dict[str, Path]],
    *,
    check_test_imports: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    pkg_root, consumers = workspace
    report = census.run_census(pkg_root=pkg_root, consumers=consumers)
    return report, census.evaluate_checks(report, check_test_imports=check_test_imports)


def _make_uv_lock(version: str) -> str:
    url = f"https://example.test/{PKG_UNDER}-{version}-py3-none-any.whl"
    return (
        f'version = 1\n\n[[package]]\nname = "{PKG_HYPHEN}"\nversion = "{version}"\n'
        f'source = {{ url = "{url}" }}\nwheels = [{{ url = "{url}", hash = "sha256:{"a" * 64}" }}]\n'
    )


def _make_runtime_env_py(extra_key: str = "", extra_spec: str = "") -> str:
    key_line = f"    {extra_key}\n" if extra_key else ""
    spec_line = f"    {extra_spec}\n" if extra_spec else ""
    return (
        "from dataclasses import dataclass\n"
        "from enum import StrEnum\n\n"
        '__all__ = ["runtime_env_contract"]\n\n'
        "class RuntimeEnvKey(StrEnum):\n"
        '    ENVIRONMENT = "ENVIRONMENT"\n'
        '    SERVICE_NAME = "OTEL_SERVICE_NAME"\n'
        '    SERVICE_VERSION = "SERVICE_VERSION"\n'
        f"{key_line}\n"
        "@dataclass(frozen=True)\n"
        "class RuntimeEnvSpec:\n"
        "    key: RuntimeEnvKey\n"
        "    aliases: tuple[str, ...] = ()\n\n"
        "RUNTIME_ENV_SPECS = (\n"
        '    RuntimeEnvSpec(RuntimeEnvKey.ENVIRONMENT, ("ENV", "APP_ENV")),\n'
        "    RuntimeEnvSpec(RuntimeEnvKey.SERVICE_NAME),\n"
        '    RuntimeEnvSpec(RuntimeEnvKey.SERVICE_VERSION, ("IMAGE_TAG",)),\n'
        f"{spec_line}"
        ")\n\n"
        "def runtime_env_contract() -> dict[str, object]:\n"
        "    return {\n"
        '        "contract_version": 2,\n'
        '        "variables": [\n'
        '            {"name": s.key.value, "aliases": list(s.aliases), "sensitive": False}\n'
        "            for s in RUNTIME_ENV_SPECS\n"
        "        ],\n"
        "    }\n"
    )


@pytest.fixture()
def synthetic_workspace(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    """Build a synthetic SDK package and two consumer repositories that pass all M1-M6 checks."""
    sdk_root = tmp_path / "sdk_repo"
    pkg_root = sdk_root / "src" / PKG_UNDER

    _write_files(
        sdk_root,
        {
            "pyproject.toml": f'[project]\nname = "{PKG_HYPHEN}"\nversion = "{VER_LATEST}"\n',
            "tests/test_readme_claims.py": 'LEFT_THE_PACKAGES = ("ENV_FALLBACK_KEYS", "CanonicalEnv", "CUSTOM_RETIRED_SYMBOL")\n',
        },
    )
    _write_files(
        pkg_root,
        {
            "__init__.py": f'from {PKG_UNDER}.alpha import used_in_prod, ProdService\n\n__all__ = ["used_in_prod", "ProdService"]\n',
            "alpha.py": (
                '_INTERNAL_CONST = "ok"\n\n'
                "def _internal_helper() -> str:\n"
                "    return _INTERNAL_CONST\n\n"
                "def used_in_prod() -> str:\n"
                "    return _internal_helper()\n\n"
                "class ProdService:\n"
                "    def ping(self) -> str:\n"
                '        return "pong"\n'
            ),
            "runtime/__init__.py": f'from {PKG_UNDER}.runtime.environment import runtime_env_contract\n\n__all__ = ["runtime_env_contract"]\n',
            "runtime/environment.py": _make_runtime_env_py(),
        },
    )

    consumer_a = tmp_path / "consumer_a"
    _write_files(
        consumer_a,
        {
            "uv.lock": _make_uv_lock(VER_LATEST),
            "app/service.py": (
                "import os\n"
                f"from {PKG_UNDER}.alpha import ProdService, used_in_prod\n"
                f"from {PKG_UNDER}.runtime.environment import runtime_env_contract\n\n"
                "def run() -> str:\n"
                '    env = os.environ.get("ENVIRONMENT") or os.environ.get("ENV") or os.environ.get("APP_ENV", "staging")\n'
                '    svc = os.environ.get("OTEL_SERVICE_NAME", "svc-a")\n'
                "    svc_obj = ProdService()\n"
                "    contract = runtime_env_contract()\n"
                '    return f"{env}:{svc}:{used_in_prod()}:{svc_obj.ping()}:{len(contract)}"\n\n'
                'if __name__ == "__main__":\n'
                "    run()\n"
            ),
            "tests/test_service.py": (
                "import os\n"
                f"from {PKG_UNDER}.alpha import used_in_prod\n\n"
                "def test_only_call() -> None:\n"
                '    os.environ["IMAGE_TAG"] = "1.0.0"\n'
                '    assert used_in_prod() == "ok"\n'
            ),
            ".github/workflows/infra-ci.yml": (
                "name: ci\n"
                "jobs:\n"
                "  check:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                f"      - run: python -m {PKG_UNDER}.alpha\n"
            ),
        },
    )

    consumer_b = tmp_path / "consumer_b"
    _write_files(
        consumer_b,
        {
            "apps/backend/uv.lock": _make_uv_lock(VER_LATEST),
            "apps/backend/main.py": (
                "import os\n"
                f"import {PKG_UNDER}.alpha as sdk_alpha\n\n"
                "def handler() -> str:\n"
                '    ver = os.environ.get("SERVICE_VERSION") or os.environ.get("IMAGE_TAG", "1.0")\n'
                '    return f"{ver}:{sdk_alpha.used_in_prod()}"\n\n'
                'if __name__ == "__main__":\n'
                "    handler()\n"
            ),
        },
    )

    return pkg_root, {"consumer_a": consumer_a, "consumer_b": consumer_b}


def test_clean_fixture_passes_all_metrics_and_cli_check(
    synthetic_workspace: tuple[Path, dict[str, Path]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pkg_root, consumers = synthetic_workspace
    out_dir = tmp_path / "out"
    argv = [
        "--sdk-src",
        str(pkg_root),
        "--repo",
        f"consumer_a={consumers['consumer_a']}",
        "--repo",
        f"consumer_b={consumers['consumer_b']}",
        "--out",
        str(out_dir),
        "--json",
        "--check",
    ]
    rc = census.main(argv)
    captured = capsys.readouterr()
    assert rc == 0, captured.out
    payload = json.loads(captured.out)
    assert payload["check"]["passed"] is True
    assert payload["check"]["failures"] == []

    m1 = payload["metrics"]["M1"]
    assert (
        m1["total_public"],
        m1["prod_used"],
        m1["test_only"],
        m1["internal_only"],
        m1["unused"],
    ) == (3, 3, 0, 0, 0)
    assert m1["missing_imports"] == {"prod": [], "test": []}
    assert m1["cli_uses"] == {f"consumer_a:{PKG_UNDER}.alpha": 1}

    m2 = payload["metrics"]["M2"]
    assert (m2["total_keys"], m2["prod_used"], m2["test_only"], m2["unused"]) == (
        6,
        6,
        0,
        0,
    )

    m3 = payload["metrics"]["M3"]
    assert m3["all_up_to_date"] is True
    assert (
        m3["consumers"]["consumer_a"]["lag"],
        m3["consumers"]["consumer_b"]["lag"],
    ) == (0, 0)

    assert (
        payload["metrics"]["M4"]["total_violations"],
        payload["metrics"]["M5"]["total_violations"],
        payload["metrics"]["M6"]["total_violations"],
        payload["metrics"]["M6"]["passed"],
    ) == (0, 0, 0, True)

    assert (out_dir / "sdk_api_reach.csv").is_file()
    assert (out_dir / "sdk_cover.csv").is_file()
    assert (out_dir / "sdk_usage_census.json").is_file()


def test_public_defs_skips_overload_stubs(tmp_path: Path) -> None:
    pkg_root = tmp_path / "src" / PKG_UNDER
    _write(
        pkg_root,
        "http.py",
        """
        import typing
        from typing import overload

        @overload
        def create_client(flag: bool = False) -> str: ...

        @typing.overload
        def create_client(flag: bool = True) -> int: ...

        def create_client(flag: bool = False) -> str | int:
            return 1 if flag else "ok"
        """,
    )
    defs = census.public_defs(pkg_root)
    assert len(defs) == 1
    assert defs[0]["name"] == "create_client"
    assert defs[0]["line"] == 10


def test_m1_mutation_fails_when_consumer_imports_missing_sdk_symbol(
    synthetic_workspace: tuple[Path, dict[str, Path]],
) -> None:
    _pkg_root, consumers = synthetic_workspace
    _write(
        consumers["consumer_a"],
        "app/broken_import.py",
        f"""
        from {PKG_UNDER}.alpha import deleted_sdk_function

        def call_deleted() -> None:
            deleted_sdk_function()
        """,
    )
    _, failures = _run_and_eval(synthetic_workspace)
    assert any(
        "M1: missing production SDK imports" in f and "deleted_sdk_function" in f
        for f in failures
    )


def test_m1_mutation_fails_on_test_import_when_tests_flag_enabled(
    synthetic_workspace: tuple[Path, dict[str, Path]],
) -> None:
    _pkg_root, consumers = synthetic_workspace
    _write(
        consumers["consumer_a"],
        "tests/test_broken.py",
        f"""
        from {PKG_UNDER}.alpha import missing_test_helper
        """,
    )
    _, prod_failures = _run_and_eval(synthetic_workspace, check_test_imports=False)
    assert prod_failures == []
    _, failures = _run_and_eval(synthetic_workspace, check_test_imports=True)
    assert any(
        "M1: missing test SDK imports" in f and "missing_test_helper" in f
        for f in failures
    )


def test_m2_mutation_fails_when_zero_contract_keys_are_used_in_prod(
    synthetic_workspace: tuple[Path, dict[str, Path]],
    tmp_path: Path,
) -> None:
    pkg_root, _consumers = synthetic_workspace
    empty_consumer = tmp_path / "empty_consumer"
    _write(empty_consumer, "uv.lock", _make_uv_lock(VER_LATEST))
    _write(empty_consumer, "app/noop.py", "x = 1\n")

    report = census.run_census(
        pkg_root=pkg_root,
        consumers={"empty_consumer": empty_consumer},
    )
    assert report["metrics"]["M2"]["prod_used"] == 0
    failures = census.evaluate_checks(report)
    assert any(
        "M2: zero runtime_env_contract() keys used in production" in f for f in failures
    )


def test_m3_mutation_fails_when_consumer_pin_lags_behind_latest_version(
    synthetic_workspace: tuple[Path, dict[str, Path]],
) -> None:
    _pkg_root, consumers = synthetic_workspace
    _write(consumers["consumer_b"], "apps/backend/uv.lock", _make_uv_lock(VER_OLD))

    report, failures = _run_and_eval(synthetic_workspace)
    m3 = report["metrics"]["M3"]
    assert m3["all_up_to_date"] is False
    assert m3["lagging_repos"] == ["consumer_b"]
    assert m3["consumers"]["consumer_b"]["lag"] == -2
    assert any(
        "M3: consumer SDK version pin lag" in f and "consumer_b=" in f for f in failures
    )


def test_m3_pyproject_wheel_and_git_tag_fallback(
    tmp_path: Path,
) -> None:
    wheel_repo, git_tag_repo = tmp_path / "wheel_repo", tmp_path / "git_tag_repo"
    _write(
        wheel_repo,
        "pyproject.toml",
        f'[project]\nname = "demo"\ndependencies = ["{PKG_HYPHEN} @ https://example.test/{PKG_UNDER}-{VER_OLD}.whl"]\n',
    )
    _write(
        git_tag_repo,
        "pyproject.toml",
        f'[project]\nname = "demo-git"\ndependencies = ["{PKG_HYPHEN} @ git+https://example.test/{PKG_HYPHEN}.git@v{VER_LATEST}"]\n',
    )

    m3 = census.compute_m3(
        {"wheel_repo": wheel_repo, "git_tag_repo": git_tag_repo},
        latest_version=VER_LATEST,
    )
    assert (
        m3["consumers"]["wheel_repo"]["pinned_version"],
        m3["consumers"]["wheel_repo"]["up_to_date"],
        m3["consumers"]["git_tag_repo"]["pinned_version"],
        m3["consumers"]["git_tag_repo"]["up_to_date"],
        census._extract_sdk_version(f"x{PKG_HYPHEN}=={VER_LATEST}"),
    ) == (VER_OLD, False, VER_LATEST, True, None)


def test_m3_missing_pin_detection(tmp_path: Path) -> None:
    no_pin = tmp_path / "no_pin"
    _write(no_pin, "app.py", "x = 1\n")
    m3 = census.compute_m3({"no_pin": no_pin}, latest_version=VER_LATEST)
    assert (
        m3["consumers"]["no_pin"]["pinned_version"],
        m3["consumers"]["no_pin"]["up_to_date"],
        m3["lagging_repos"],
    ) == (None, False, ["no_pin"])


def test_m1_ignores_module_dunder_attributes_with_uninstalled_sdk_sdk_src(
    synthetic_workspace: tuple[Path, dict[str, Path]],
) -> None:
    pkg_root, consumers = synthetic_workspace
    _write(
        consumers["consumer_a"],
        "app/dunder_access.py",
        f"""
        import {PKG_UNDER}
        from {PKG_UNDER} import __version__

        def locate_pkg() -> str:
            return f"{{getattr({PKG_UNDER}, '__file__', '')}}:{{{PKG_UNDER}.__file__}}:{{{PKG_UNDER}.__name__}}:{{__version__}}"

        if __name__ == "__main__":
            locate_pkg()
        """,
    )
    report = census.run_census(pkg_root=pkg_root, consumers=consumers)
    assert report["metrics"]["M1"]["missing_imports"] == {"prod": [], "test": []}
    assert census.evaluate_checks(report, check_test_imports=True) == []


def test_m4_mutation_fails_when_consumer_references_left_the_packages_symbol(
    synthetic_workspace: tuple[Path, dict[str, Path]],
) -> None:
    _pkg_root, consumers = synthetic_workspace
    _write_files(
        consumers["consumer_a"],
        {
            "tests/test_legacy_ref.py": f"""
                from {PKG_UNDER}.runtime import ENV_FALLBACK_KEYS, CUSTOM_RETIRED_SYMBOL

                assert ENV_FALLBACK_KEYS is not None
                assert CUSTOM_RETIRED_SYMBOL is not None
            """,
        },
    )
    _write_files(
        consumers["consumer_b"],
        {
            "apps/backend/star_ref.py": f"""
                from {PKG_UNDER}.runtime import *

                val = CanonicalEnv
            """,
        },
    )
    report, failures = _run_and_eval(synthetic_workspace)
    m4 = report["metrics"]["M4"]
    assert m4["total_violations"] == 3
    assert m4["by_repo"] == {"consumer_a": 2, "consumer_b": 1}
    assert any("M4:" in f and "ENV_FALLBACK_KEYS" in f for f in failures)


def test_m4_dict_left_the_packages_and_wheel_smoke_removed_names(
    synthetic_workspace: tuple[Path, dict[str, Path]],
) -> None:
    pkg_root, consumers = synthetic_workspace
    sdk_root = pkg_root.parent.parent
    _write_files(
        sdk_root,
        {
            "tests/test_readme_claims.py": f"""
                LEFT_THE_PACKAGES = {{
                    "{PKG_UNDER}": {{
                        "alpha": ["ProdService"],
                    }},
                }}
                REMOVED_IN_3_5_1 = (
                    ("{PKG_UNDER}.alpha", "removed_351_helper"),
                    ("{PKG_UNDER}.alpha", "main"),
                )
            """,
            "tests/wheel_smoke.py": f"""
                REMOVED_NAMES = (
                    ("{PKG_UNDER}.alpha", "removed_340_helper"),
                    ("{PKG_UNDER}.alpha", "ProdService.from_env"),
                )
            """,
        },
    )

    # Submodule import of ProdService (from infra2_sdk.alpha import ProdService) is valid:
    clean_report, _ = _run_and_eval(synthetic_workspace)
    assert clean_report["metrics"]["M4"]["total_violations"] == 0

    # Package-level import of ProdService and imports of REMOVED_NAMES / REMOVED_IN_3_5_1 fail M4:
    _write_files(
        consumers["consumer_a"],
        {
            "tests/test_dict_left.py": f"""
                from {PKG_UNDER} import ProdService
                from {PKG_UNDER}.alpha import removed_340_helper, removed_351_helper, main
            """,
        },
    )
    report, _ = _run_and_eval(synthetic_workspace)
    m4 = report["metrics"]["M4"]
    assert m4["total_violations"] == 4
    flagged_names = {v["name"] for v in m4["violations"]}
    assert flagged_names == {
        "ProdService",
        "removed_340_helper",
        "removed_351_helper",
        "main",
    }


def test_m4_mutation_fails_on_residual_libs_tools_lib_and_sdk_import_error_fallback(
    synthetic_workspace: tuple[Path, dict[str, Path]],
) -> None:
    _pkg_root, consumers = synthetic_workspace
    _write_files(
        consumers["consumer_a"],
        {
            "libs/legacy.py": "x = 1\n",
            "tools/_lib/helper.py": "y = 2\n",
        },
    )
    _write_files(
        consumers["consumer_b"],
        {
            "common/fallback.py": f"""
                try:
                    from {PKG_UNDER}.alpha import used_in_prod
                except ImportError:
                    def used_in_prod() -> str:
                        return "fallback"
            """,
        },
    )
    report, failures = _run_and_eval(synthetic_workspace)
    m4 = report["metrics"]["M4"]
    assert m4["total_violations"] == 3
    names = {v["name"] for v in m4["violations"]}
    assert names == {"libs/", "tools/_lib/", "except_ImportError_fallback"}
    assert any("M4:" in f for f in failures)


def test_m5_mutation_fails_on_hardcoded_service_name_collection_dict_lookup_and_manifest(
    synthetic_workspace: tuple[Path, dict[str, Path]],
) -> None:
    _pkg_root, consumers = synthetic_workspace
    _write_files(
        consumers["consumer_a"],
        {
            "app/scrub.py": """
                SCRUB_KEYS = {"SERVICE_NAME", "SERVICE_VERSION", "OTEL_SERVICE_NAME"}
                SCRUB_DEFAULTS = {"SERVICE_NAME": "a", "OTEL_SERVICE_NAME": "b"}
            """,
        },
    )
    _write_files(
        consumers["consumer_b"],
        {
            "apps/backend/logger.py": """
                import os

                def resolve_name() -> str:
                    return os.getenv("OTEL_SERVICE_NAME") or os.getenv("SERVICE_NAME") or "default"
            """,
            "apps/backend/required-env.generated.json": json.dumps(
                {"fields": [{"env": "SERVICE_NAME", "source": "runtime"}]}
            ),
        },
    )

    report, failures = _run_and_eval(synthetic_workspace)
    m5 = report["metrics"]["M5"]
    assert m5["total_violations"] == 4
    kinds = {v["kind"] for v in m5["violations"]}
    assert kinds == {"hardcoded_collection", "env_lookup", "manifest_service_name"}
    assert any("M5:" in f and "hardcoded SERVICE_NAME/OTEL" in f for f in failures)


def test_m4_mutation_fails_on_pure_reexport_shim_in_common(
    synthetic_workspace: tuple[Path, dict[str, Path]],
) -> None:
    _pkg_root, consumers = synthetic_workspace
    _write_files(
        consumers["consumer_a"],
        {
            "common/deploy/git_provenance.py": f"""
                from __future__ import annotations
                from {PKG_UNDER}.alpha import used_in_prod

                __all__ = ["used_in_prod"]
            """,
        },
    )
    report, failures = _run_and_eval(synthetic_workspace)
    m4 = report["metrics"]["M4"]
    assert m4["total_violations"] == 1
    assert m4["violations"][0]["name"] == "pure_reexport_shim"
    assert m4["violations"][0]["site"] == "consumer_a:common/deploy/git_provenance.py:1"
    assert any("M4:" in f and "pure_reexport_shim" in f for f in failures)


def test_m6_mutation_fails_when_tools_script_imports_from_tools(
    synthetic_workspace: tuple[Path, dict[str, Path]],
) -> None:
    _pkg_root, consumers = synthetic_workspace
    _write_files(
        consumers["consumer_a"],
        {
            "tools/cli_a.py": """
                from tools.cli_b import helper
                import tools.cli_c as c

                def main() -> None:
                    helper()
            """,
        },
    )
    report, failures = _run_and_eval(synthetic_workspace)
    m6 = report["metrics"]["M6"]
    assert m6["passed"] is False
    assert m6["total_violations"] == 2
    assert any("M6:" in f and "tools.cli_b" in f for f in failures)


def test_m6_mutation_fails_on_wildcard_reexport_sys_modules_hijack_and_reverse_common_to_tools(
    synthetic_workspace: tuple[Path, dict[str, Path]],
) -> None:
    _pkg_root, consumers = synthetic_workspace
    _write_files(
        consumers["consumer_a"],
        {
            "tools/hijack_cli.py": """
                import sys
                from .local_helper import run
                from common.deploy import v2 as _v2
                from common.deploy.v2 import *

                if __name__ == "__main__":
                    load_tool("other_cli")
                    raise SystemExit(_v2.main())
                else:
                    sys.modules[__name__] = _v2
            """,
        },
    )
    _write_files(
        consumers["consumer_b"],
        {
            "common/deploy/bad_reverse.py": """
                from tools import deploy_v2
            """,
        },
    )
    report, failures = _run_and_eval(synthetic_workspace)
    m6 = report["metrics"]["M6"]
    assert m6["passed"] is False
    imported = {v["import"] for v in m6["violations"]}
    assert ".local_helper" in imported
    assert "common.deploy.v2.*" in imported
    assert "sys.modules[__name__]" in imported
    assert "load_tool()" in imported
    assert "tools" in imported
    assert any("M6:" in f for f in failures)


def test_m1_and_m2_check_fails_when_non_prod_symbols_or_keys_exist(
    synthetic_workspace: tuple[Path, dict[str, Path]],
) -> None:
    pkg_root, _consumers = synthetic_workspace
    _write_files(
        pkg_root,
        {
            "extra.py": 'def unreferenced_public_fn() -> str:\n    return "dead"\n',
            "__init__.py": f'from {PKG_UNDER}.alpha import used_in_prod, ProdService\nfrom {PKG_UNDER}.extra import unreferenced_public_fn\n\n__all__ = ["used_in_prod", "ProdService", "unreferenced_public_fn"]\n',
            "runtime/environment.py": _make_runtime_env_py(
                'DEAD_KEY = "OTEL_TRACES_SAMPLER"',
                "RuntimeEnvSpec(RuntimeEnvKey.DEAD_KEY),",
            ),
        },
    )
    _, failures = _run_and_eval(synthetic_workspace)
    assert any(
        "M1: non-production SDK symbols" in f and "unreferenced_public_fn" in f
        for f in failures
    )
    assert any(
        "M2: non-production runtime_env_contract() keys" in f
        and "OTEL_TRACES_SAMPLER" in f
        for f in failures
    )


@pytest.mark.parametrize(
    "flags",
    [
        ["--repo", "missing=/nonexistent"],
        ["--repo", "="],
        ["--sdk-src", "/no_such_sdk"],
    ],
)
def test_cli_invalid_repo_or_sdk_src_exits_with_code_2(
    tmp_path: Path, flags: list[str]
) -> None:
    argv = [f"{arg}{tmp_path}" if arg == "=" else arg for arg in flags]
    with pytest.raises(SystemExit) as exc:
        census.main(argv)
    assert exc.value.code == 2


def test_human_summary_and_nonzero_exit_on_check_failure(
    synthetic_workspace: tuple[Path, dict[str, Path]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    pkg_root, consumers = synthetic_workspace
    _write(
        consumers["consumer_a"],
        "app/bad.py",
        """
        BAD_LIST = ("SERVICE_NAME", "OTEL_SERVICE_NAME", "ENVIRONMENT")
        """,
    )
    rc = census.main(
        [
            "--sdk-src",
            str(pkg_root),
            "--repo",
            f"consumer_a={consumers['consumer_a']}",
            "--check",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "CHECK FAILURES:" in out
    assert "M5:" in out


def test_m1_downgrades_uncalled_or_test_only_consumer_class_method_sdk_use(
    synthetic_workspace: tuple[Path, dict[str, Path]],
) -> None:
    pkg_root, consumers = synthetic_workspace
    _write_files(
        pkg_root,
        {
            "zombie.py": (
                'def dead_method_sym() -> str:\n    return "dead"\n\n'
                'def test_method_sym() -> str:\n    return "test"\n\n'
                'def dead_func_sym() -> str:\n    return "dead_fn"\n'
            ),
            "__init__.py": f'from {PKG_UNDER}.alpha import used_in_prod, ProdService\nfrom {PKG_UNDER}.zombie import dead_func_sym, dead_method_sym, test_method_sym\n\n__all__ = ["used_in_prod", "ProdService", "dead_method_sym", "test_method_sym", "dead_func_sym"]\n',
        },
    )
    _write_files(
        consumers["consumer_a"],
        {
            "app/model.py": (
                f"from {PKG_UNDER}.zombie import dead_func_sym, dead_method_sym, test_method_sym\n\n"
                "def never_called_top_level_helper() -> str:\n    return dead_func_sym()\n\n"
                "class SettingsModel:\n"
                "    def never_called_anywhere(self) -> str:\n        return dead_method_sym()\n\n"
                "    def only_called_in_test(self) -> str:\n        return test_method_sym()\n"
            ),
            "tests/test_model.py": "from app.model import SettingsModel\n\ndef test_it() -> None:\n    assert SettingsModel().only_called_in_test() == 'test'\n",
        },
    )
    report, _ = _run_and_eval(synthetic_workspace)
    by_name = {s["name"]: s["reach"] for s in report["metrics"]["M1"]["symbols"]}
    assert by_name["dead_method_sym"] == "unused"
    assert by_name["dead_func_sym"] == "unused"
    assert by_name["test_method_sym"] == "test_only"


def test_m1_ignores_empty_dunder_all_and_credits_cli_main_call_sites(
    synthetic_workspace: tuple[Path, dict[str, Path]],
) -> None:
    pkg_root, consumers = synthetic_workspace
    _write_files(
        pkg_root,
        {
            "cli_tool.py": '__all__ = ()\n\ndef cli_entry_helper() -> str:\n    return "ran"\n\ndef unreferenced_in_main() -> str:\n    return "idle"\n\ndef main() -> int:\n    cli_entry_helper()\n    return 0\n',
            "__init__.py": f'from {PKG_UNDER}.alpha import used_in_prod, ProdService\nfrom {PKG_UNDER}.cli_tool import cli_entry_helper, unreferenced_in_main\n\n__all__ = ["used_in_prod", "ProdService", "cli_entry_helper", "unreferenced_in_main"]\n',
        },
    )
    _write_files(
        consumers["consumer_a"],
        {
            ".github/workflows/infra-ci.yml": f"name: cli\njobs:\n  run:\n    runs-on: ubuntu-latest\n    steps:\n      - run: python -m {PKG_UNDER}.cli_tool\n",
        },
    )
    report, _ = _run_and_eval(synthetic_workspace)
    by_name = {s["name"]: s["reach"] for s in report["metrics"]["M1"]["symbols"]}
    assert by_name["cli_entry_helper"] == "prod_used"
    assert by_name["unreferenced_in_main"] == "unused"


def test_m2_credits_canonical_key_when_registered_alias_is_used_in_prod(
    synthetic_workspace: tuple[Path, dict[str, Path]],
    tmp_path: Path,
) -> None:
    pkg_root, _consumers = synthetic_workspace
    alias_consumer = tmp_path / "alias_consumer"
    _write_files(
        alias_consumer,
        {
            "uv.lock": _make_uv_lock(VER_LATEST),
            "app/worker.py": 'import os\n\ndef tag() -> str:\n    return os.environ.get("IMAGE_TAG", "dev")\n',
        },
    )
    report = census.run_census(
        pkg_root=pkg_root,
        consumers={"alias_consumer": alias_consumer},
    )
    by_key = {k["key"]: k["reach"] for k in report["metrics"]["M2"]["keys"]}
    assert by_key["IMAGE_TAG"] == "prod_used"
    assert by_key["SERVICE_VERSION"] == "prod_used"


def test_m1_audits_package_dunder_all_even_when_submodule_dunder_all_hides_symbol(
    synthetic_workspace: tuple[Path, dict[str, Path]],
) -> None:
    """#1410: package `__init__.py` `__all__` is the public contract; a symbol omitted from
    its submodule's `__all__` or an exported submodule in package `__all__` must be visible."""
    pkg_root, _consumers = synthetic_workspace
    _write_files(
        pkg_root,
        {
            "hidden_sub.py": '__all__ = ["visible_sub_fn"]\n\ndef visible_sub_fn() -> str:\n    return "sub"\n\ndef reexported_only_in_pkg() -> str:\n    return "pkg"\n',
            "__init__.py": f'from {PKG_UNDER} import alpha\nfrom {PKG_UNDER}.alpha import used_in_prod, ProdService\nfrom {PKG_UNDER}.hidden_sub import reexported_only_in_pkg\n\n__all__ = ["alpha", "used_in_prod", "ProdService", "reexported_only_in_pkg"]\n',
        },
    )
    report, failures = _run_and_eval(synthetic_workspace)
    m1 = report["metrics"]["M1"]
    assert m1["contract_source"] == "package_dunder_all"
    by_name = {s["name"]: s for s in m1["symbols"]}
    assert "visible_sub_fn" not in by_name
    assert by_name["alpha"]["kind"] == "module"
    assert by_name["alpha"]["reach"] == "prod_used"
    assert by_name["reexported_only_in_pkg"]["reach"] == "unused"
    assert any("reexported_only_in_pkg" in f for f in failures)


def test_m1_verifies_surface_json_consumer_claims_and_honors_documented_and_held(
    synthetic_workspace: tuple[Path, dict[str, Path]],
) -> None:
    """#1410: M1 honors `documented:` and `held:` entries in `tests/surface.json` while
    failing on stale `consumer:` claims whose named consumer has 0 production AST imports."""
    pkg_root, _consumers = synthetic_workspace
    sdk_root = pkg_root.parent.parent
    surface_data = {
        PKG_UNDER: {
            "used_in_prod": "consumer: consumer_a, consumer_b",
            "ProdService": "consumer: consumer_a, consumer_b",
            "doc_entrypoint": "documented: README section Core",
            "HELD_CONST": "held: decision 7",
        },
        f"{PKG_UNDER}.runtime": {
            "runtime_env_contract": "consumer: consumer_a",
        },
    }
    _write_files(
        pkg_root,
        {
            "alpha.py": 'def used_in_prod() -> str:\n    return "ok"\n\nclass ProdService:\n    def ping(self) -> str:\n        return "pong"\n\ndef doc_entrypoint() -> str:\n    return "doc"\n\nHELD_CONST = 1\n',
            "__init__.py": f'from {PKG_UNDER}.alpha import HELD_CONST, ProdService, doc_entrypoint, used_in_prod\n\n__all__ = ["used_in_prod", "ProdService", "doc_entrypoint", "HELD_CONST"]\n',
        },
    )
    _write(sdk_root, "tests/surface.json", json.dumps(surface_data))
    report, failures = _run_and_eval(synthetic_workspace)
    m1 = report["metrics"]["M1"]
    assert m1["surface_justifications"] == {"consumer": 3, "documented": 1, "held": 1}
    # ProdService is used in consumer_a only, NOT in consumer_b -> stale claim!
    assert len(m1["stale_consumer_claims"]) == 1
    assert m1["stale_consumer_claims"][0]["name"] == "ProdService"
    assert m1["stale_consumer_claims"][0]["missing_consumers"] == ["consumer_b"]
    assert any(
        "stale surface.json consumer claim" in f and "ProdService" in f
        for f in failures
    )
    # Documented and held symbols are honored (not flagged as non-production SDK symbols)
    assert not any("non-production SDK symbols" in f for f in failures)

    # Fix surface.json claim -> passes cleanly
    surface_data[PKG_UNDER]["ProdService"] = "consumer: consumer_a"
    _write(sdk_root, "tests/surface.json", json.dumps(surface_data))
    _, clean_failures = _run_and_eval(synthetic_workspace)
    assert clean_failures == []


def test_live_infra2_repository_census_contract() -> None:
    report = census.run_census(consumers={"infra2": census.REPO_ROOT})
    metrics = report["metrics"]
    assert metrics["M1"]["contract_source"] == "package_dunder_all"
    assert metrics["M1"]["total_public"] >= 50
    assert metrics["M1"]["prod_used"] >= 20
    assert metrics["M1"]["missing_imports"]["prod"] == []
    assert metrics["M2"]["total_keys"] >= 12
    assert (
        metrics["M2"]["prod_used"],
        metrics["M2"]["test_only"],
        metrics["M2"]["unused"],
    ) == (metrics["M2"]["total_keys"], 0, 0)
    assert metrics["M3"]["consumers"]["infra2"]["up_to_date"] is True
    assert metrics["M3"]["consumers"]["infra2"]["lag"] == 0
    assert (
        metrics["M4"]["total_violations"],
        metrics["M5"]["total_violations"],
        metrics["M6"]["total_violations"],
        metrics["M6"]["passed"],
    ) == (0, 0, 0, True)
    assert census.evaluate_checks(report) == []
