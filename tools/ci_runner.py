#!/usr/bin/env python3
"""Unified single-source-of-truth runner for CI gate checks and local verification.

This module provides a command line interface to execute identical checks locally
and in GitHub Actions CI workflows. It adheres to ASD-STE100 principles:
- Clear and short sentences.
- Active voice.
- Deterministic exit codes: 0 on success, non-zero on failure.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Sequence

ROOT = Path(__file__).resolve().parent.parent

# Dummy environment variables for compose validation matching CI requirements.
DUMMY_COMPOSE_ENV: dict[str, str] = {
    "DATA_PATH": "/tmp/infra2-data",
    "ENV": "ci",
    "ENV_SUFFIX": "-ci",
    "ENV_DOMAIN_SUFFIX": "ci",
    "INTERNAL_DOMAIN": "internal.example.test",
    "VAULT_ADDR": "http://vault.example.test:8200",
    "FEISHU_APP_ID": "dummy-feishu-app-id",
    "FEISHU_APP_SECRET": "dummy-feishu-app-secret",
    "FEISHU_VERIFICATION_TOKEN": "dummy-feishu-verification-token",
    "FEISHU_ENCRYPT_KEY": "dummy-feishu-encrypt-key",
    "LLM_API_KEY": "dummy-llm-api-key",
    "DATA_ENGINE_IMAGE_DIGEST": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
    "RELEASE_MANIFEST_ID": "release-manifest:0000000000000000000000000000000000000000000000000000000000000000",
    "CONFIGURATION_SHA256": "0000000000000000000000000000000000000000000000000000000000000000",
    "CAPTURE_APPROVED_BY": "ci:compose-validation",
    "TA_POSTGRES_PORT": "15432",
    "DAGSTER_WEBSERVER_PORT": "13001",
    "OP_CONNECT_TOKEN": "dummy-op-connect-token",
    "OP_VAULT_ID": "dummy-op-vault-id",
    "OP_ITEM_ID": "dummy-op-item-id",
}


def _run_cmd(
    cmd: Sequence[str],
    *,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
    check: bool = True,
    capture_output: bool = False,
    set_pythonpath: bool = True,
    timeout: float | None = 120,
) -> subprocess.CompletedProcess[str]:
    """Execute a system command and return the process result."""
    merged_env = os.environ.copy()
    if set_pythonpath:
        existing_pp = merged_env.get("PYTHONPATH", "")
        merged_env["PYTHONPATH"] = f"{ROOT}:{existing_pp}" if existing_pp else str(ROOT)
    else:
        merged_env.pop("PYTHONPATH", None)
    if env:
        merged_env.update(env)
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=merged_env,
        check=check,
        capture_output=capture_output,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


def run_preflight(verbose: bool = False) -> int:
    """Run fast SSOT index generation, gate audits, and contract preflight checks.

    These checks take less than 10 seconds total. They fail fast on drift.
    """
    print("▶ Running preflight static and drift gates...")
    start_time = time.monotonic()

    gates: list[tuple[str, Sequence[str]]] = [
        ("SSOT README index", [sys.executable, "tools/gen_ssot_index.py"]),
        ("Project README index", [sys.executable, "tools/gen_project_index.py"]),
        (
            "CI gate inventory audit",
            [sys.executable, "-m", "tools.ci_gate_audit", "--enforce"],
        ),
        (
            "CI gate hierarchy lint",
            [sys.executable, "-m", "tools.ci_gate_lint", ".github/workflows/"],
        ),
        (
            "Watchdog consistency audit",
            [sys.executable, "tools/watchdog_consistency_audit.py"],
        ),
        ("No-new-wheels lint", [sys.executable, "tools/no_new_wheels_lint.py"]),
        (
            "Service identity audit",
            [sys.executable, "tools/service_identity_audit.py"],
        ),
        ("Deploy guard audit", [sys.executable, "tools/deploy_guard_audit.py"]),
        (
            "OMCA gate policy self-test",
            [sys.executable, "-m", "tools.omca_gate_policy", "--self-test"],
        ),
        (
            "Backup inventory audit",
            [sys.executable, "tools/backup_inventory_audit.py"],
        ),
        (
            "Deployer contract light preflight",
            [sys.executable, "libs/tests/test_deployer_contract_light.py"],
        ),
    ]

    failed: list[str] = []
    for name, cmd in gates:
        sub_start = time.monotonic()
        try:
            _run_cmd(cmd, capture_output=not verbose)
            elapsed = time.monotonic() - sub_start
            print(f"  ✅ {name} passed ({elapsed:.2f}s)")
        except subprocess.CalledProcessError as exc:
            elapsed = time.monotonic() - sub_start
            print(f"  ❌ {name} failed ({elapsed:.2f}s)")
            if exc.stdout:
                print(exc.stdout)
            if exc.stderr:
                print(exc.stderr, file=sys.stderr)
            failed.append(name)

    total_time = time.monotonic() - start_time
    if failed:
        print(
            f"❌ Preflight failed: {len(failed)} check(s) failed in {total_time:.2f}s"
        )
        return 1

    print(f"✅ Preflight passed: all {len(gates)} checks passed in {total_time:.2f}s")
    return 0


def run_compose(verbose: bool = False) -> int:
    """Validate all compose files syntax, resource limits, and image pins."""
    print("▶ Validating compose files and resource limits...")
    start_time = time.monotonic()

    # 1. Discover compose files
    compose_files: list[Path] = []
    for pattern in ("compose.yaml", "compose.yml"):
        for path in ROOT.glob(f"**/{pattern}"):
            path_str = str(path)
            if any(skip in path_str for skip in (".venv", "node_modules", ".git")):
                continue
            compose_files.append(path)

    compose_files.sort()
    print(f"  Found {len(compose_files)} compose file(s) to validate.")

    # 2. Validate YAML syntax for all compose files
    print("  Validating compose YAML syntax...")
    import yaml

    failed_yaml: list[Path] = []
    for file in compose_files:
        try:
            with open(file, "r", encoding="utf-8") as f:
                yaml.safe_load(f)
        except Exception as exc:
            print(f"  ❌ Compose syntax error in {file.relative_to(ROOT)}: {exc}")
            failed_yaml.append(file)
    if failed_yaml:
        print(f"❌ {len(failed_yaml)} compose file(s) failed YAML syntax validation.")
        return 1

    # 3. Check docker compose config if docker is available
    has_docker = shutil.which("docker") is not None
    if has_docker:
        try:
            probe = subprocess.run(
                ["docker", "compose", "version"],
                capture_output=True,
                text=True,
                timeout=5,
                stdin=subprocess.DEVNULL,
            )
            if probe.returncode != 0:
                has_docker = False
        except (subprocess.SubprocessError, OSError):
            has_docker = False

    if has_docker:
        print("  Docker compose is available. Validating configurations with docker...")
        failed_compose: list[Path] = []
        for file in compose_files:
            rel = file.relative_to(ROOT)
            cmd = ["docker", "compose", "-f", str(rel), "config"]
            res = _run_cmd(
                cmd,
                env=DUMMY_COMPOSE_ENV,
                check=False,
                capture_output=not verbose,
            )
            if res.returncode != 0:
                print(f"  ❌ Invalid compose configuration: {rel}")
                if res.stderr:
                    print(res.stderr, file=sys.stderr)
                failed_compose.append(rel)
            elif verbose:
                print(f"  ✅ Valid compose: {rel}")

        if failed_compose:
            print(f"❌ {len(failed_compose)} compose file(s) failed validation.")
            return 1
    else:
        print(
            "  ℹ️ Docker daemon not running. Verified YAML syntax; skipped live docker compose config."
        )

    # 4. Lint compose resource limits and image pins
    print("  Running compose resource limits and image pins lint...")
    try:
        _run_cmd([sys.executable, "tools/lint_compose_resource_limits.py"])
        print("  ✅ Compose resource limits and image pins verified.")
    except subprocess.CalledProcessError as exc:
        print("  ❌ Compose resource limits lint failed.")
        if exc.stdout:
            print(exc.stdout)
        if exc.stderr:
            print(exc.stderr, file=sys.stderr)
        return 1

    total_time = time.monotonic() - start_time
    print(f"✅ Compose validation passed in {total_time:.2f}s.")
    return 0


def run_deployers(verbose: bool = False) -> int:
    """Validate Deployer class definitions and service facet completeness matrix."""
    print("▶ Validating Deployer classes and service facets...")
    start_time = time.monotonic()

    # 1. Discover all deploy.py files excluding third-party submodules
    deploy_files: list[Path] = []
    for path in ROOT.glob("**/deploy.py"):
        path_str = str(path)
        if any(
            skip in path_str
            for skip in (".venv", "node_modules", ".git", "build", "dist", "repos")
        ):
            continue
        deploy_files.append(path)

    deploy_files.sort()
    print(f"  Found {len(deploy_files)} deploy.py file(s).")

    failed: list[tuple[Path, list[str]]] = []
    for deploy_file in deploy_files:
        rel = deploy_file.relative_to(ROOT)
        try:
            spec = importlib.util.spec_from_file_location("deploy_module", deploy_file)
            if not spec or not spec.loader:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules["deploy_module"] = module
            try:
                spec.loader.exec_module(module)
            finally:
                sys.modules.pop("deploy_module", None)

            # Discover Deployer subclasses
            deployer_cls = None
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    isinstance(attr, type)
                    and attr.__name__ != "Deployer"
                    and "Deployer" in [base.__name__ for base in attr.__mro__]
                ):
                    deployer_cls = attr
                    break

            if deployer_cls:
                errors: list[str] = []
                service_name = getattr(deployer_cls, "service", None)
                if not service_name:
                    errors.append("missing service name")
                compose_path_str = getattr(deployer_cls, "compose_path", None)
                if not compose_path_str:
                    errors.append("missing compose_path")
                else:
                    compose_path = ROOT / compose_path_str
                    if not compose_path.exists():
                        errors.append(f"compose file not found: {compose_path_str}")

                if errors:
                    failed.append((rel, errors))
                    print(f"  ❌ {rel}: {', '.join(errors)}")
                elif verbose:
                    print(f"  ✅ {deployer_cls.__name__}: service={service_name}")
            elif verbose:
                print(f"  ⚠️ {rel}: No Deployer subclass found")

        except Exception as exc:
            failed.append((rel, [str(exc)]))
            print(f"  ❌ {rel}: {exc}")

    if failed:
        print(f"❌ {len(failed)} deploy.py file(s) have errors.")
        return 1

    print("  ✅ All Deployer classes valid.")

    # 2. Run service facet completeness matrix
    print("  Checking service facet completeness matrix...")
    try:
        _run_cmd([sys.executable, "-m", "tools.service_facet_matrix"])
    except subprocess.CalledProcessError as exc:
        print("  ❌ Service facet matrix failed.")
        if exc.stdout:
            print(exc.stdout)
        return 1

    total_time = time.monotonic() - start_time
    print(f"✅ Deployer validation passed in {total_time:.2f}s.")
    return 0


def run_vault(
    agents_only: bool = False, policy_only: bool = False, verbose: bool = False
) -> int:
    """Validate Vault agent settings and Vault policy syntax."""
    print("▶ Validating Vault configurations...")
    start_time = time.monotonic()
    failed = False

    # 1. Validate vault-agent.hcl and compose healthchecks
    if not policy_only:
        print("  Validating vault-agent.hcl files and healthchecks...")
        agent_files = [
            p
            for p in ROOT.glob("**/vault-agent.hcl")
            if not any(s in str(p) for s in (".venv", "node_modules", ".git"))
        ]
        for hcl in agent_files:
            rel = hcl.relative_to(ROOT)
            try:
                content = hcl.read_text(encoding="utf-8")
            except OSError as exc:
                print(f"  ❌ Cannot read {rel}: {exc}")
                failed = True
                continue
            if "exit_on_err = true" not in content:
                print(f"  ❌ Missing 'exit_on_err = true' in {rel}")
                failed = True
            if "exit_on_retry_failure = true" not in content:
                print(f"  ❌ Missing 'exit_on_retry_failure = true' in {rel}")
                failed = True

        compose_files = [
            p
            for p in ROOT.glob("**/compose.yaml")
            if not any(s in str(p) for s in (".venv", "node_modules", ".git"))
        ]
        for compose in compose_files:
            rel = compose.relative_to(ROOT)
            try:
                content = compose.read_text(encoding="utf-8")
            except OSError as exc:
                print(f"  ❌ Cannot read {rel}: {exc}")
                failed = True
                continue
            if "vault-agent:" not in content:
                continue
            if "rm -f /vault/secrets/.env" not in content:
                print(f"  ❌ Vault agent does not clear stale secrets in {rel}")
                failed = True
            if "vault token lookup" not in content:
                print(f"  ❌ Vault agent healthcheck missing token lookup in {rel}")
                failed = True
            if "<no value>" not in content:
                print(
                    f"  ❌ Vault agent healthcheck does not reject <no value> in {rel}"
                )
                failed = True
            if re.search(r"VAULT_AGENT_MAX_SECRET_AGE_SECONDS|stat -c %Y", content):
                print(f"  ❌ Vault agent must not use mtime freshness in {rel}")
                failed = True

        if failed:
            print("❌ Vault agent validation failed.")
            return 1
        print("  ✅ All vault-agent.hcl and compose settings valid.")

    # 2. Validate vault-policy.hcl syntax
    if not agents_only:
        print("  Validating vault-policy.hcl syntax...")
        policy_files = [
            p
            for p in ROOT.glob("**/vault-policy.hcl")
            if not any(s in str(p) for s in (".venv", "node_modules", ".git"))
        ]
        hcl2_installed = False
        try:
            import hcl2  # type: ignore

            hcl2_installed = True
        except ImportError:
            hcl2_installed = False

        for pol in policy_files:
            rel = pol.relative_to(ROOT)
            if hcl2_installed:
                try:
                    with open(pol, "r", encoding="utf-8") as f:
                        hcl2.load(f)
                    if verbose:
                        print(f"  ✅ Valid HCL: {rel}")
                except Exception as exc:
                    print(f"  ❌ Invalid HCL in {rel}: {exc}")
                    failed = True
            else:
                try:
                    content = pol.read_text(encoding="utf-8").strip()
                except OSError as exc:
                    print(f"  ❌ Cannot read {rel}: {exc}")
                    failed = True
                    continue
                if not content:
                    print(f"  ❌ Empty policy file: {rel}")
                    failed = True
                elif content.count("{") != content.count("}"):
                    print(f"  ❌ Unbalanced braces in policy file: {rel}")
                    failed = True

        if failed:
            print("❌ Vault policy validation failed.")
            return 1
        if not hcl2_installed:
            print("  ℹ️ python-hcl2 not installed. Verified basic HCL structure.")
        print("  ✅ All vault-policy.hcl files valid.")

    total_time = time.monotonic() - start_time
    print(f"✅ Vault validation passed in {total_time:.2f}s.")
    return 0


def run_harness(verbose: bool = False) -> int:
    """Validate workspace harness consistency."""
    print("▶ Validating workspace harness...")
    start_time = time.monotonic()
    try:
        _run_cmd(
            [
                sys.executable,
                "-m",
                "tools.harness",
                "check",
                "--no-submodules-expected",
            ]
        )
        total_time = time.monotonic() - start_time
        print(f"✅ Harness validation passed in {total_time:.2f}s.")
        return 0
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            print(exc.stdout)
        if exc.stderr:
            print(exc.stderr, file=sys.stderr)
        return 1


def run_lint(verbose: bool = False) -> int:
    """Run ruff linter, ruff formatter, and anti-tautology test linter."""
    print("▶ Running Python linter and format checks...")
    start_time = time.monotonic()

    # 1. ruff check
    print("  Running ruff check...")
    try:
        _run_cmd(
            [
                "ruff",
                "check",
                "libs/",
                "bootstrap/",
                "platform/",
                "finance_report/",
                "truealpha/",
                "tools/",
            ]
        )
        print("  ✅ ruff check passed.")
    except subprocess.CalledProcessError:
        print("  ❌ ruff check failed.")
        return 1

    # 2. ruff format check on changed files
    base_ref = os.environ.get("GITHUB_BASE_REF", "main")
    changed_py_files: set[str] = set()

    # Check working tree changes (both unstaged and staged)
    try:
        wt_diff = subprocess.run(
            [
                "git",
                "diff",
                "--name-only",
                "HEAD",
                "--",
                "libs",
                "bootstrap",
                "platform",
                "finance_report",
                "truealpha",
                "tools",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        for line in wt_diff.stdout.splitlines():
            if line.endswith(".py") and (ROOT / line).exists():
                changed_py_files.add(line)
    except Exception as exc:
        print(f"  ⚠️ Warning: git diff HEAD check failed: {exc}")

    # Check committed branch changes against base
    try:
        branch_diff = subprocess.run(
            [
                "git",
                "diff",
                "--name-only",
                f"origin/{base_ref}...HEAD",
                "--",
                "libs",
                "bootstrap",
                "platform",
                "finance_report",
                "truealpha",
                "tools",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if branch_diff.returncode == 0:
            for line in branch_diff.stdout.splitlines():
                if line.endswith(".py") and (ROOT / line).exists():
                    changed_py_files.add(line)
    except Exception as exc:
        print(f"  ⚠️ Warning: git diff origin/{base_ref}...HEAD check failed: {exc}")

    sorted_files = sorted(changed_py_files)
    if sorted_files:
        print(f"  Checking format on {len(sorted_files)} changed Python file(s)...")
        try:
            _run_cmd(["ruff", "format", "--check", *sorted_files])
            print("  ✅ ruff format passed.")
        except subprocess.CalledProcessError:
            print("  ❌ ruff format failed.")
            return 1
    else:
        print("  No changed Python files detected for format check.")

    # 3. anti-tautology test linter
    print("  Running anti-tautology test linter...")
    targets = ["libs/tests"]
    if (ROOT / "repos/infra2-sdk/tests").is_dir():
        targets.append("repos/infra2-sdk/tests")
    try:
        _run_cmd([sys.executable, "tools/lint_anti_tautology.py", *targets])
        print("  ✅ anti-tautology test linter passed.")
    except subprocess.CalledProcessError:
        print("  ❌ anti-tautology test linter failed.")
        return 1

    total_time = time.monotonic() - start_time
    print(f"✅ Python linting passed in {total_time:.2f}s.")
    return 0


def run_unit_tests(verbose: bool = False, timeout: float = 600.0) -> int:
    """Run full unit test suite with coverage and isolation."""
    print("▶ Running infra unit test suite...")
    start_time = time.monotonic()

    # 1. Fetch manifests
    _run_cmd([sys.executable, "tools/fetch_app_manifests.py"])

    # 2. Pytest execution
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "libs/tests",
        "-q",
        "-n",
        "auto",
    ]
    env = {"PYTHONSAFEPATH": "1"}
    try:
        _run_cmd(cmd, env=env, set_pythonpath=False, timeout=timeout)
        total_time = time.monotonic() - start_time
        print(f"✅ Unit tests passed in {total_time:.2f}s.")
        return 0
    except subprocess.TimeoutExpired:
        print(f"❌ Unit tests timed out after {timeout}s.")
        return 1
    except subprocess.CalledProcessError:
        print("❌ Unit tests failed.")
        return 1


GATES: dict[str, tuple[str, Callable[..., int]]] = {
    "preflight": (
        "Run fast SSOT index generation, gate audits, and contract preflight checks",
        run_preflight,
    ),
    "compose": (
        "Validate compose files syntax, resource limits, and image pins",
        run_compose,
    ),
    "deployers": (
        "Validate Deployer class definitions and service facet completeness matrix",
        run_deployers,
    ),
    "vault": ("Validate Vault agent settings and policy syntax", run_vault),
    "harness": ("Validate workspace harness consistency", run_harness),
    "lint": (
        "Run ruff linter, ruff formatter, and anti-tautology test linter",
        run_lint,
    ),
    "unit-tests": (
        "Run full unit test suite with coverage and isolation",
        run_unit_tests,
    ),
}


def run_all(
    verbose: bool = False,
    fail_fast: bool = True,
    with_unit_tests: bool = False,
) -> int:
    """Run all primary gate targets sequentially."""
    print("🚀 Running all CI gate targets...")
    suite = ["preflight", "compose", "deployers", "vault", "harness", "lint"]
    if with_unit_tests:
        suite.append("unit-tests")

    failed = False
    for target in suite:
        _, fn = GATES[target]
        ret = fn(verbose=verbose)
        if ret != 0:
            failed = True
            if fail_fast:
                print(f"❌ Stop on first failure in target '{target}'.")
                return 1

    if failed:
        print("❌ One or more CI gate targets failed.")
        return 1

    print("🎉 All CI gate targets passed successfully!")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entrypoint for CI runner."""
    parser = argparse.ArgumentParser(
        description="Unified SSOT runner for CI gate checks and local verification."
    )
    parser.add_argument(
        "target",
        nargs="?",
        default="all",
        choices=[*GATES.keys(), "all"],
        help="Target gate suite to execute (default: all).",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose step output."
    )
    parser.add_argument(
        "--no-fail-fast",
        dest="fail_fast",
        action="store_false",
        default=True,
        help="Do not stop on first failure when running all targets.",
    )
    parser.add_argument(
        "--with-unit-tests",
        action="store_true",
        help="When running all targets, also execute the full unit test suite.",
    )
    parser.add_argument(
        "--agents-only",
        action="store_true",
        help="For vault target: validate vault-agent settings only.",
    )
    parser.add_argument(
        "--policy-only",
        action="store_true",
        help="For vault target: validate vault-policy syntax only.",
    )
    parser.add_argument(
        "--list", action="store_true", help="List all available targets and exit."
    )

    args = parser.parse_args(argv)

    if args.list:
        print("Available CI gate targets:")
        for name, (desc, _) in GATES.items():
            print(f"  {name:12} - {desc}")
        print("  all          - Run all primary gate targets sequentially")
        return 0

    if args.target == "all":
        return run_all(
            verbose=args.verbose,
            fail_fast=args.fail_fast,
            with_unit_tests=args.with_unit_tests,
        )

    if args.target == "vault":
        return run_vault(
            agents_only=args.agents_only,
            policy_only=args.policy_only,
            verbose=args.verbose,
        )

    _, fn = GATES[args.target]
    return fn(verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
