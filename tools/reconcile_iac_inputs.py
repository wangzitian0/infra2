#!/usr/bin/env python3
"""Reconcile IaC-pinned services whose deploy inputs changed between release tags.

This is the release-tag promotion layer for input-drift reconciliation: diff the
previous release tag -> the promoted tag -> deploy dependency fan-out -> deploy_v2/
iac_runner per affected service (pinned to the tag) -> Deployer config-hash gate decides
no-op vs restart. staging/prod accept tags only, so the promoted ref is always a tag.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from libs.deploy_dependencies import explain_fanout
from tools.deploy_contract import all_service_keys, service_spec

MANIFEST_PATH = "docs/ssot/deploy-dependencies.yaml"


@dataclass(frozen=True)
class ReconcilePlan:
    changed_files: list[str]
    selected: dict[str, str]
    ignored: dict[str, str]
    dropped: list[str]
    staging_services: list[str]
    prod_services: list[str]

    @property
    def services(self) -> list[str]:
        return sorted(set(self.staging_services) | set(self.prod_services))

    def to_dict(self) -> dict:
        return {
            "changed_files": self.changed_files,
            "selected": self.selected,
            "ignored": self.ignored,
            "dropped": self.dropped,
            "services": self.services,
            "staging_services": self.staging_services,
            "prod_services": self.prod_services,
        }


@dataclass(frozen=True)
class DeployCommand:
    service: str
    deploy_type: str
    argv: list[str]

    def to_dict(self) -> dict:
        return {
            "service": self.service,
            "type": self.deploy_type,
            "argv": self.argv,
        }


def is_zero_sha(value: str | None) -> bool:
    return bool(value) and set(value.strip()) == {"0"}


def assert_after_on_main(
    after: str,
    repo_root: Path,
    *,
    base: str = "origin/main",
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    """Fail-closed provenance guard: the promoted tag MUST be reachable from main.

    A ``v*.*.*`` tag push drives a REAL staging/prod deploy, so the tagged commit must
    be on reviewed ``origin/main``. This enforces the Infra-011 invariant — *iac_pinned
    production reconcile may run automatically only from reviewed infra2 main* — in code,
    and blocks the v1.1.16 incident where a release tag cut on an unmerged, off-main
    feature branch promoted a pre-refactor ref straight to prod. ``--dry-run`` callers
    skip this (plan-only, no deploy). Returns the resolved 40-hex commit.
    """
    resolved = runner(
        ["git", "rev-parse", "--verify", f"{after}^{{commit}}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if resolved.returncode != 0:
        raise SystemExit(
            f"::error::cannot resolve {after!r} to a commit "
            f"({resolved.stderr.strip() or 'unknown revision'})."
        )
    sha = resolved.stdout.strip()
    ancestor = runner(
        ["git", "merge-base", "--is-ancestor", sha, base],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if ancestor.returncode != 0:
        raise SystemExit(
            f"::error::refusing to reconcile {after!r} ({sha[:12]}): not reachable from "
            f"{base}. Release tags must be cut on reviewed main (Infra-011 invariant). "
            f"Re-cut the tag on main, or pass --dry-run to plan only."
        )
    return sha


def changed_files_from_git(
    repo_root: Path, before: str | None, after: str
) -> list[str]:
    """Return changed files between two release tags (``before``..``after``).

    ``before`` is the previous release tag; when it is empty/all-zero (the first
    release has no predecessor) diff the tagged commit itself instead of a
    nonexistent range.
    """

    if before and not is_zero_sha(before):
        cmd = ["git", "diff", "--name-only", before, after]
    else:
        cmd = ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", after]
    result = subprocess.run(
        cmd,
        cwd=repo_root,
        capture_output=True,
        check=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def iac_pinned_services() -> list[str]:
    services: list[str] = []
    for service in all_service_keys():
        spec = service_spec(service)
        if spec.iac_pinned:
            services.append(service)
    return sorted(services)


def build_plan(changed_files: Sequence[str]) -> ReconcilePlan:
    files = sorted(dict.fromkeys(str(path) for path in changed_files if str(path)))
    decision = explain_fanout(files)
    selected: dict[str, str] = {}
    ignored: dict[str, str] = {}

    if MANIFEST_PATH in files:
        for service in iac_pinned_services():
            selected.setdefault(
                service, f"dependency manifest changed ({MANIFEST_PATH})"
            )

    for service, reason in sorted(decision.selected.items()):
        try:
            spec = service_spec(service)
        except ValueError:
            ignored[service] = "not registered in deploy_v2 service registry"
            continue
        if not spec.iac_pinned:
            ignored[service] = (
                "not iac_pinned; fixed app deploys need an explicit version_ref"
            )
            continue
        selected[service] = reason

    staging_services: list[str] = []
    prod_services: list[str] = []
    for service in sorted(selected):
        spec = service_spec(service)
        if spec.prod_only:
            prod_services.append(service)
        else:
            staging_services.append(service)
            prod_services.append(service)

    return ReconcilePlan(
        changed_files=files,
        selected=dict(sorted(selected.items())),
        ignored=dict(sorted(ignored.items())),
        dropped=sorted(decision.dropped),
        staging_services=staging_services,
        prod_services=prod_services,
    )


def build_deploy_commands(
    plan: ReconcilePlan,
    *,
    iac_ref: str,
    domain: str,
    timeout: int,
    python_executable: str = sys.executable,
) -> list[DeployCommand]:
    commands: list[DeployCommand] = []
    for deploy_type, services in (
        ("staging", plan.staging_services),
        ("prod", plan.prod_services),
    ):
        if not services:
            continue
        argv = [
            python_executable,
            "-m",
            "tools.deploy_v2",
            "--service",
            ",".join(services),
            "--type",
            deploy_type,
            # iac_pinned services ignore version_ref (their artifact IS the iac_ref stack),
            # but staging/prod accept tags only — pin both axes to the promoted release tag
            # so the command is self-consistent and never carries a moving ref to a fixed env.
            "--version-ref",
            iac_ref,
            "--iac-ref",
            iac_ref,
            "--domain",
            domain,
            "--timeout",
            str(timeout),
        ]
        if deploy_type == "prod":
            argv.append("--code-reviewed")
            if set(services) & set(plan.staging_services):
                argv.append("--staging-validated")
        commands.append(
            DeployCommand(
                service=",".join(services), deploy_type=deploy_type, argv=argv
            )
        )
    return commands


def run_deploy_commands(
    commands: Sequence[DeployCommand],
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[dict]:
    results: list[dict] = []
    for command in commands:
        completed = runner(
            command.argv,
            capture_output=True,
            text=True,
        )
        record = {
            "service": command.service,
            "type": command.deploy_type,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
        if completed.returncode == 0 and completed.stdout.strip():
            try:
                record["deploy_v2"] = json.loads(completed.stdout)
            except json.JSONDecodeError:
                record["deploy_v2_parse_error"] = "stdout was not JSON"
        results.append(record)
        if completed.returncode != 0:
            break
    return results


def write_summary(
    *,
    plan: ReconcilePlan,
    commands: Sequence[DeployCommand],
    results: Sequence[dict],
    dry_run: bool,
    path: str | None = None,
) -> None:
    target = path or os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        return
    lines = [
        "## IaC Input Reconcile",
        "",
        f"- Mode: `{'dry-run' if dry_run else 'apply'}`",
        f"- Changed files: `{len(plan.changed_files)}`",
        f"- Selected services: `{', '.join(plan.services) if plan.services else 'none'}`",
        f"- Dropped files: `{len(plan.dropped)}`",
        f"- Commands: `{len(commands)}`",
    ]
    if plan.ignored:
        lines.append(
            "- Ignored services: "
            + ", ".join(
                f"`{service}` ({reason})" for service, reason in plan.ignored.items()
            )
        )
    if results:
        failed = [item for item in results if item.get("returncode") != 0]
        lines.append(
            f"- Deploy results: `{len(results) - len(failed)} ok / {len(failed)} failed`"
        )
    lines.append("")
    lines.append("```json")
    lines.append(
        json.dumps({"plan": plan.to_dict(), "results": list(results)}, indent=2)
    )
    lines.append("```")
    with open(target, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="reconcile changed IaC service inputs")
    parser.add_argument("--repo-root", default=".", help="git repository root")
    parser.add_argument(
        "--before",
        default="",
        help="previous release tag (base for changed-file detection)",
    )
    parser.add_argument(
        "--after", required=True, help="release tag to promote (iac_ref)"
    )
    parser.add_argument(
        "--domain", default=os.environ.get("INTERNAL_DOMAIN", "zitian.party")
    )
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument(
        "--dry-run", action="store_true", help="plan only; do not deploy"
    )
    parser.add_argument(
        "--changed-file",
        action="append",
        default=[],
        help="explicit changed file; bypasses git diff when provided",
    )
    parser.add_argument("--output-json", default="", help="optional output JSON path")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    # Fail-closed BEFORE planning/deploying: an apply run must promote a tag that is
    # reachable from reviewed main (Infra-011). dry-run is plan-only, so it may inspect
    # any ref (e.g. preview a not-yet-merged tag).
    if not args.dry_run:
        assert_after_on_main(args.after, repo_root)
    changed_files = (
        list(args.changed_file)
        if args.changed_file
        else changed_files_from_git(repo_root, args.before or None, args.after)
    )
    plan = build_plan(changed_files)
    commands = build_deploy_commands(
        plan,
        iac_ref=args.after,
        domain=args.domain,
        timeout=args.timeout,
    )
    results: list[dict] = []
    if not args.dry_run and commands:
        results = run_deploy_commands(commands)

    payload = {
        "plan": plan.to_dict(),
        "commands": [command.to_dict() for command in commands],
        "results": results,
        "dry_run": args.dry_run,
    }
    print(json.dumps(payload, sort_keys=True))
    if args.output_json:
        Path(args.output_json).write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
    write_summary(plan=plan, commands=commands, results=results, dry_run=args.dry_run)
    return 1 if any(result.get("returncode") != 0 for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
