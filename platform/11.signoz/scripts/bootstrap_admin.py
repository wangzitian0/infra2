#!/usr/bin/env python3
"""Helper script to reset SigNoz metadata and recreate admin accounts via Invoke."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _run_command(command: list[str], env: dict[str, str], cwd: Path) -> None:
    try:
        subprocess.run(command, env=env, cwd=cwd, check=True)
    except subprocess.CalledProcessError as exc:
        print(f"Command failed: {' '.join(command)} (exit {exc.returncode})", file=sys.stderr)
        raise


def bootstrap(env_name: str, reset: bool) -> None:
    """Run SigNoz reset and admin tasks for the given environment."""
    env = os.environ.copy()
    env["DEPLOY_ENV"] = env_name

    if reset:
        print(f"==> Resetting SigNoz metadata for {env_name}")
        _run_command(["uv", "run", "invoke", "signoz.shared.reset-metadata"], env=env, cwd=REPO_ROOT)
    print(f"==> Ensuring SigNoz admin for {env_name}")
    _run_command(["uv", "run", "invoke", "signoz.shared.ensure-admin"], env=env, cwd=REPO_ROOT)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run SigNoz admin bootstrap tasks via Invoke")
    parser.add_argument(
        "-e",
        "--env",
        choices=["staging", "production"],
        default="staging",
        help="Target environment name",
    )
    parser.add_argument(
        "-r",
        "--reset",
        action="store_true",
        help="Reset metadata before creating the admin (destroys users/dashboards)",
    )
    args = parser.parse_args()

    try:
        bootstrap(args.env, reset=args.reset)
    except subprocess.CalledProcessError:
        sys.exit(1)


if __name__ == "__main__":
    main()
