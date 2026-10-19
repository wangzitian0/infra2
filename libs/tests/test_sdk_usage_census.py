"""Fixture, mutation, and repository contract tests for tools/sdk_usage_census.py (#1246)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from tools import sdk_usage_census as census

PKG_HYPHEN = "infra2" + "-sdk"
PKG_UNDER = "infra2" + "_sdk"
VER_LATEST = "9.8.7"
VER_OLD = "9.6.5"


def _write(root: Path, rel: str, content: str) -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    return target


def _make_uv_lock(version: str) -> str:
    wheel_name = f"{PKG_UNDER}-{version}-py3-none-any.whl"
    digest = "a" * 64
    return (
        "version = 1\n\n"
        "[[package]]\n"
        f'name = "{PKG_HYPHEN}"\n'
        f'version = "{version}"\n'
        f'source = {{ url = "https://example.test/{wheel_name}" }}\n'
        f'wheels = [{{ url = "https://example.test/{wheel_name}", '
        f'hash = "sha256:{digest}" }}]\n'
    )


@pytest.fixture()
def synthetic_workspace(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    """Build a synthetic SDK package and two consumer repositories that pass all M1-M5 checks."""
    sdk_root = tmp_path / "sdk_repo"
    pkg_root = sdk_root / "src" / PKG_UNDER

    _write(
        sdk_root,
        "pyproject.toml",
        f"""
        [project]
        name = "{PKG_HYPHEN}"
        version = "{VER_LATEST}"
        """,
    )
    _write(
        pkg_root,
        "__init__.py",
        f"""
        from {PKG_UNDER}.alpha import used_in_prod, used_in_test_only, internal_helper, unused_func

        __all__ = ["used_in_prod", "used_in_test_only", "internal_helper", "unused_func"]
        """,
    )
    _write(
        pkg_root,
        "alpha.py",
        """
        PUBLIC_CONST = "ok"
        TYPED_CONST: str = "typed"

        def internal_helper() -> str:
            return PUBLIC_CONST

        def used_in_prod() -> str:
            return internal_helper()

        def used_in_test_only() -> str:
            return "test"

        def unused_func() -> str:
            return "unused"

        class ProdService:
            def ping(self) -> str:
                return "pong"
        """,
    )
    _write(
        pkg_root,
        "runtime/__init__.py",
        f"""
        from {PKG_UNDER}.runtime.environment import RuntimeEnvKey, runtime_env_contract

        __all__ = ["RuntimeEnvKey", "runtime_env_contract"]
        """,
    )
    _write(
        pkg_root,
        "runtime/environment.py",
        """
        from dataclasses import dataclass
        from enum import StrEnum

        class RuntimeEnvKey(StrEnum):
            ENVIRONMENT = "ENVIRONMENT"
            SERVICE_NAME = "OTEL_SERVICE_NAME"
            SERVICE_VERSION = "SERVICE_VERSION"
            UNUSED_KEY = "OTEL_TRACES_SAMPLER"

        @dataclass(frozen=True)
        class RuntimeEnvSpec:
            key: RuntimeEnvKey
            aliases: tuple[str, ...] = ()

        RUNTIME_ENV_SPECS = (
            RuntimeEnvSpec(RuntimeEnvKey.ENVIRONMENT, ("ENV", "APP_ENV")),
            RuntimeEnvSpec(RuntimeEnvKey.SERVICE_NAME),
            RuntimeEnvSpec(RuntimeEnvKey.SERVICE_VERSION, ("IMAGE_TAG",)),
            RuntimeEnvSpec(RuntimeEnvKey.UNUSED_KEY),
        )

        def runtime_env_contract() -> dict[str, object]:
            return {
                "contract_version": 2,
                "variables": [
                    {"name": s.key.value, "aliases": list(s.aliases), "sensitive": False}
                    for s in RUNTIME_ENV_SPECS
                ],
            }
        """,
    )
    _write(
        sdk_root,
        "tests/test_readme_claims.py",
        """
        LEFT_THE_PACKAGES = (
            "ENV_FALLBACK_KEYS",
            "CanonicalEnv",
            "CUSTOM_RETIRED_SYMBOL",
        )
        """,
    )

    consumer_a = tmp_path / "consumer_a"
    _write(consumer_a, "uv.lock", _make_uv_lock(VER_LATEST))
    _write(
        consumer_a,
        "app/service.py",
        f"""
        import os
        from {PKG_UNDER}.alpha import ProdService, used_in_prod

        def run() -> str:
            env = os.environ.get("ENVIRONMENT", "staging")
            svc = os.environ.get("OTEL_SERVICE_NAME", "svc-a")
            svc_obj = ProdService()
            return f"{{env}}:{{svc}}:{{used_in_prod()}}:{{svc_obj.ping()}}"
        """,
    )
    _write(
        consumer_a,
        "tests/test_service.py",
        f"""
        import os
        from {PKG_UNDER}.alpha import used_in_test_only

        def test_only_call() -> None:
            os.environ["IMAGE_TAG"] = "1.0.0"
            assert used_in_test_only() == "test"
        """,
    )
    _write(
        consumer_a,
        ".github/workflows/infra-ci.yml",
        f"""
        name: ci
        jobs:
          check:
            runs-on: ubuntu-latest
            steps:
              - run: python -m {PKG_UNDER}.alpha
        """,
    )

    consumer_b = tmp_path / "consumer_b"
    _write(consumer_b, "apps/backend/uv.lock", _make_uv_lock(VER_LATEST))
    _write(
        consumer_b,
        "apps/backend/main.py",
        f"""
        import os
        import {PKG_UNDER}.alpha as sdk_alpha

        def handler() -> str:
            ver = os.environ.get("SERVICE_VERSION", "1.0")
            return f"{{ver}}:{{sdk_alpha.used_in_prod()}}"
        """,
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
    assert m1["total_public"] == 11
    assert m1["prod_used"] == 2  # used_in_prod, ProdService
    assert m1["test_only"] == 1  # used_in_test_only
    assert (
        m1["internal_only"] == 5
    )  # internal_helper, PUBLIC_CONST, RuntimeEnvKey, RuntimeEnvSpec, RUNTIME_ENV_SPECS
    assert m1["unused"] == 3  # unused_func, TYPED_CONST, runtime_env_contract
    assert m1["missing_imports"] == {"prod": [], "test": []}
    assert m1["cli_uses"] == {f"consumer_a:{PKG_UNDER}.alpha": 1}

    m2 = payload["metrics"]["M2"]
    assert m2["total_keys"] == 7  # 4 canonical + 3 aliases
    assert m2["prod_used"] == 3  # ENVIRONMENT, OTEL_SERVICE_NAME, SERVICE_VERSION
    assert m2["test_only"] == 1  # IMAGE_TAG
    assert m2["unused"] == 3  # ENV, APP_ENV, OTEL_TRACES_SAMPLER

    m3 = payload["metrics"]["M3"]
    assert m3["all_up_to_date"] is True
    assert m3["consumers"]["consumer_a"]["lag"] == 0
    assert m3["consumers"]["consumer_b"]["lag"] == 0

    assert payload["metrics"]["M4"]["total_violations"] == 0
    assert payload["metrics"]["M5"]["total_violations"] == 0

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
    pkg_root, consumers = synthetic_workspace
    _write(
        consumers["consumer_a"],
        "app/broken_import.py",
        f"""
        from {PKG_UNDER}.alpha import deleted_sdk_function

        def call_deleted() -> None:
            deleted_sdk_function()
        """,
    )
    report = census.run_census(pkg_root=pkg_root, consumers=consumers)
    failures = census.evaluate_checks(report)
    assert any(
        "M1: missing production SDK imports" in f and "deleted_sdk_function" in f
        for f in failures
    )


def test_m1_mutation_fails_on_test_import_when_tests_flag_enabled(
    synthetic_workspace: tuple[Path, dict[str, Path]],
) -> None:
    pkg_root, consumers = synthetic_workspace
    _write(
        consumers["consumer_a"],
        "tests/test_broken.py",
        f"""
        from {PKG_UNDER}.alpha import missing_test_helper
        """,
    )
    report = census.run_census(pkg_root=pkg_root, consumers=consumers)
    assert census.evaluate_checks(report, check_test_imports=False) == []
    failures = census.evaluate_checks(report, check_test_imports=True)
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
    pkg_root, consumers = synthetic_workspace
    _write(consumers["consumer_b"], "apps/backend/uv.lock", _make_uv_lock(VER_OLD))

    report = census.run_census(pkg_root=pkg_root, consumers=consumers)
    m3 = report["metrics"]["M3"]
    assert m3["all_up_to_date"] is False
    assert m3["lagging_repos"] == ["consumer_b"]
    assert m3["consumers"]["consumer_b"]["lag"] == -2
    failures = census.evaluate_checks(report)
    assert any(
        "M3: consumer SDK version pin lag" in f and "consumer_b=" in f for f in failures
    )


def test_m3_pyproject_wheel_and_git_tag_fallback(
    tmp_path: Path,
) -> None:
    wheel_repo = tmp_path / "wheel_repo"
    _write(
        wheel_repo,
        "pyproject.toml",
        f"""
        [project]
        name = "demo"
        dependencies = ["{PKG_HYPHEN} @ https://example.test/{PKG_UNDER}-{VER_OLD}.whl"]
        """,
    )
    git_tag_repo = tmp_path / "git_tag_repo"
    _write(
        git_tag_repo,
        "pyproject.toml",
        f"""
        [project]
        name = "demo-git"
        dependencies = ["{PKG_HYPHEN} @ git+https://example.test/{PKG_HYPHEN}.git@v{VER_LATEST}"]
        """,
    )

    m3 = census.compute_m3(
        {"wheel_repo": wheel_repo, "git_tag_repo": git_tag_repo},
        latest_version=VER_LATEST,
    )
    assert m3["consumers"]["wheel_repo"]["pinned_version"] == VER_OLD
    assert m3["consumers"]["wheel_repo"]["up_to_date"] is False
    assert m3["consumers"]["git_tag_repo"]["pinned_version"] == VER_LATEST
    assert m3["consumers"]["git_tag_repo"]["up_to_date"] is True
    assert census._extract_sdk_version(f"x{PKG_HYPHEN}=={VER_LATEST}") is None


def test_m3_missing_pin_detection(tmp_path: Path) -> None:
    no_pin = tmp_path / "no_pin"
    _write(no_pin, "app.py", "x = 1\n")

    m3 = census.compute_m3({"no_pin": no_pin}, latest_version=VER_LATEST)
    assert m3["consumers"]["no_pin"]["pinned_version"] is None
    assert m3["consumers"]["no_pin"]["up_to_date"] is False
    assert m3["lagging_repos"] == ["no_pin"]


def test_m1_ignores_module_dunder_attributes_with_uninstalled_sdk_src(
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
        """,
    )
    report = census.run_census(pkg_root=pkg_root, consumers=consumers)
    assert report["metrics"]["M1"]["missing_imports"] == {"prod": [], "test": []}
    assert census.evaluate_checks(report, check_test_imports=True) == []


def test_m4_mutation_fails_when_consumer_references_left_the_packages_symbol(
    synthetic_workspace: tuple[Path, dict[str, Path]],
) -> None:
    pkg_root, consumers = synthetic_workspace
    _write(
        consumers["consumer_a"],
        "tests/test_legacy_ref.py",
        f"""
        from {PKG_UNDER}.runtime import ENV_FALLBACK_KEYS, CUSTOM_RETIRED_SYMBOL

        assert ENV_FALLBACK_KEYS is not None
        assert CUSTOM_RETIRED_SYMBOL is not None
        """,
    )
    _write(
        consumers["consumer_b"],
        "apps/backend/star_ref.py",
        f"""
        from {PKG_UNDER}.runtime import *

        val = CanonicalEnv
        """,
    )
    report = census.run_census(pkg_root=pkg_root, consumers=consumers)
    m4 = report["metrics"]["M4"]
    assert m4["total_violations"] == 3
    assert m4["by_repo"] == {"consumer_a": 2, "consumer_b": 1}
    failures = census.evaluate_checks(report)
    assert any("M4:" in f and "ENV_FALLBACK_KEYS" in f for f in failures)


def test_m4_dict_left_the_packages_and_wheel_smoke_removed_names(
    synthetic_workspace: tuple[Path, dict[str, Path]],
) -> None:
    pkg_root, consumers = synthetic_workspace
    sdk_root = pkg_root.parent.parent
    _write(
        sdk_root,
        "tests/test_readme_claims.py",
        f"""
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
    )
    _write(
        sdk_root,
        "tests/wheel_smoke.py",
        f"""
        REMOVED_NAMES = (
            ("{PKG_UNDER}.alpha", "removed_340_helper"),
            ("{PKG_UNDER}.alpha", "ProdService.from_env"),
        )
        """,
    )

    # Submodule import of ProdService (from infra2_sdk.alpha import ProdService) is valid:
    clean_report = census.run_census(pkg_root=pkg_root, consumers=consumers)
    assert clean_report["metrics"]["M4"]["total_violations"] == 0

    # Package-level import of ProdService and imports of REMOVED_NAMES / REMOVED_IN_3_5_1 fail M4:
    _write(
        consumers["consumer_a"],
        "tests/test_dict_left.py",
        f"""
        from {PKG_UNDER} import ProdService
        from {PKG_UNDER}.alpha import removed_340_helper, removed_351_helper, main
        """,
    )
    report = census.run_census(pkg_root=pkg_root, consumers=consumers)
    m4 = report["metrics"]["M4"]
    assert m4["total_violations"] == 4
    flagged_names = {v["name"] for v in m4["violations"]}
    assert flagged_names == {
        "ProdService",
        "removed_340_helper",
        "removed_351_helper",
        "main",
    }


def test_m5_mutation_fails_on_hardcoded_service_name_collection_dict_lookup_and_manifest(
    synthetic_workspace: tuple[Path, dict[str, Path]],
) -> None:
    pkg_root, consumers = synthetic_workspace
    _write(
        consumers["consumer_a"],
        "app/scrub.py",
        """
        SCRUB_KEYS = {"SERVICE_NAME", "SERVICE_VERSION", "OTEL_SERVICE_NAME"}
        SCRUB_DEFAULTS = {"SERVICE_NAME": "a", "OTEL_SERVICE_NAME": "b"}
        """,
    )
    _write(
        consumers["consumer_b"],
        "apps/backend/logger.py",
        """
        import os

        def resolve_name() -> str:
            return os.getenv("OTEL_SERVICE_NAME") or os.getenv("SERVICE_NAME") or "default"
        """,
    )
    _write(
        consumers["consumer_b"],
        "apps/backend/required-env.generated.json",
        json.dumps({"fields": [{"env": "SERVICE_NAME", "source": "runtime"}]}),
    )

    report = census.run_census(pkg_root=pkg_root, consumers=consumers)
    m5 = report["metrics"]["M5"]
    assert m5["total_violations"] == 4
    kinds = {v["kind"] for v in m5["violations"]}
    assert kinds == {"hardcoded_collection", "env_lookup", "manifest_service_name"}
    failures = census.evaluate_checks(report)
    assert any("M5:" in f and "hardcoded SERVICE_NAME/OTEL" in f for f in failures)


def test_cli_invalid_repo_or_sdk_src_exits_with_code_2(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc_repo:
        census.main(["--repo", f"missing={tmp_path / 'nonexistent'}"])
    assert exc_repo.value.code == 2

    with pytest.raises(SystemExit) as exc_empty_name:
        census.main(["--repo", f"={tmp_path}"])
    assert exc_empty_name.value.code == 2

    with pytest.raises(SystemExit) as exc_sdk:
        census.main(["--sdk-src", str(tmp_path / "no_such_sdk")])
    assert exc_sdk.value.code == 2


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
    _write(
        pkg_root,
        "zombie.py",
        """
        def dead_method_sym() -> str:
            return "dead"

        def test_method_sym() -> str:
            return "test"
        """,
    )
    _write(
        consumers["consumer_a"],
        "app/model.py",
        f"""
        from {PKG_UNDER}.zombie import dead_method_sym, test_method_sym

        class SettingsModel:
            def never_called_anywhere(self) -> str:
                return dead_method_sym()

            def only_called_in_test(self) -> str:
                return test_method_sym()
        """,
    )
    _write(
        consumers["consumer_a"],
        "tests/test_model.py",
        """
        from app.model import SettingsModel

        def test_it() -> None:
            assert SettingsModel().only_called_in_test() == "test"
        """,
    )
    report = census.run_census(pkg_root=pkg_root, consumers=consumers)
    by_name = {s["name"]: s["reach"] for s in report["metrics"]["M1"]["symbols"]}
    assert by_name["dead_method_sym"] == "unused"
    assert by_name["test_method_sym"] == "test_only"


def test_m1_ignores_empty_dunder_all_and_credits_cli_main_call_sites(
    synthetic_workspace: tuple[Path, dict[str, Path]],
) -> None:
    pkg_root, consumers = synthetic_workspace
    _write(
        pkg_root,
        "cli_tool.py",
        """
        __all__ = ()

        def cli_entry_helper() -> str:
            return "ran"

        def unreferenced_in_main() -> str:
            return "idle"

        def main() -> int:
            cli_entry_helper()
            return 0
        """,
    )
    _write(
        consumers["consumer_a"],
        ".github/workflows/infra-ci.yml",
        f"""
        name: cli
        jobs:
          run:
            runs-on: ubuntu-latest
            steps:
              - run: python -m {PKG_UNDER}.cli_tool
        """,
    )
    report = census.run_census(pkg_root=pkg_root, consumers=consumers)
    by_name = {s["name"]: s["reach"] for s in report["metrics"]["M1"]["symbols"]}
    assert by_name["cli_entry_helper"] == "prod_used"
    assert by_name["unreferenced_in_main"] == "unused"


def test_m2_credits_canonical_key_when_registered_alias_is_used_in_prod(
    synthetic_workspace: tuple[Path, dict[str, Path]],
    tmp_path: Path,
) -> None:
    pkg_root, _consumers = synthetic_workspace
    alias_consumer = tmp_path / "alias_consumer"
    _write(alias_consumer, "uv.lock", _make_uv_lock(VER_LATEST))
    _write(
        alias_consumer,
        "app/worker.py",
        """
        import os

        def tag() -> str:
            return os.environ.get("IMAGE_TAG", "dev")
        """,
    )
    report = census.run_census(
        pkg_root=pkg_root,
        consumers={"alias_consumer": alias_consumer},
    )
    by_key = {k["key"]: k["reach"] for k in report["metrics"]["M2"]["keys"]}
    assert by_key["IMAGE_TAG"] == "prod_used"
    assert by_key["SERVICE_VERSION"] == "prod_used"


def test_live_infra2_repository_census_contract() -> None:
    report = census.run_census(consumers={"infra2": census.REPO_ROOT})
    metrics = report["metrics"]
    assert metrics["M1"]["total_public"] >= 50
    assert metrics["M1"]["prod_used"] >= 20
    assert metrics["M1"]["missing_imports"]["prod"] == []
    assert metrics["M2"]["total_keys"] >= 12
    assert metrics["M2"]["prod_used"] >= 8
    assert metrics["M3"]["consumers"]["infra2"]["up_to_date"] is True
    assert metrics["M3"]["consumers"]["infra2"]["lag"] == 0
    assert metrics["M4"]["total_violations"] == 0
    assert metrics["M5"]["total_violations"] == 0
    assert census.evaluate_checks(report) == []
