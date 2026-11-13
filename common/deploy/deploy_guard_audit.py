#!/usr/bin/env python3
"""Guard the deploy dependency graph against silent under-fan-out.

Static audit (no live API): for every service compose file, enumerates the
files its config hash reads (Dockerfile, COPY/ADD sources against the build
context, bind mounts) and verifies that each one outside the service directory
fans out to the service through docs/ssot/deploy-dependencies.yaml. An input
that fans out to nothing is an under-fan-out landmine: a change to it would not
redeploy the service, leaving it on stale input (#267's alerting gap, #1117).

Exits non-zero on violation, so it serves as both a PR gate (infra-ci) and a
scheduled monitor (ops-checks deploy-guard-audit task alerts on the non-zero exit).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.deploy.config_hash import (  # noqa: E402
    _compose_artifact_files,
    _compose_has_build,
    compose_host_mount_files,
    compose_mount_fingerprint_gaps,
    mount_fingerprint_var,
    parse_compose_services,
)
from common.deploy.dependencies import (  # noqa: E402
    CANONICAL_SERVICE_ROOTS,
    config_hash_input_count,
    fanout_coverage_violations,
    service_key_from_path,
)

# Canonical service roots whose layout service_key_from_path understands.
_CANONICAL_SERVICE_ROOTS = CANONICAL_SERVICE_ROOTS
_SERVICE_ROOTS = _CANONICAL_SERVICE_ROOTS
_REQUIRED_SERVICE_ROOTS = _CANONICAL_SERVICE_ROOTS
_COMPOSE_GLOB = "*compose*.y*ml"

# Minimum (compose_count, input_count) per service root (#1123).
# Prevents silent drop when a service directory is renamed without a dot,
# `_SERVICE_ROOTS` is truncated, or bind mounts/contexts are rewritten to bare `${VAR}`.
_SERVICE_ROOT_FLOORS: dict[str, tuple[int, int]] = {
    "platform": (13, 350),
    "finance_report": (4, 8),
    "truealpha": (4, 9),
    "bootstrap": (4, 12),
}


def _iter_compose_files(root: Path, service_roots: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    for service_root in service_roots:
        base = root / service_root
        if not base.is_dir():
            continue
        for compose in sorted(base.rglob(_COMPOSE_GLOB)):
            if compose.is_file():
                found.append(compose)
    return found


def find_service_composes(root: Path = ROOT) -> list[Path]:
    """Every compose file in a service directory (including `*compose*.y*ml` templates)."""
    return [
        compose
        for compose in _iter_compose_files(root, _SERVICE_ROOTS)
        if service_key_from_path(compose.relative_to(root).as_posix())
    ]


def find_unmapped_composes(root: Path = ROOT) -> list[str]:
    """Compose files under `_REQUIRED_SERVICE_ROOTS` that `service_key_from_path` maps to None."""
    return [
        compose.relative_to(root).as_posix()
        for compose in _iter_compose_files(root, _REQUIRED_SERVICE_ROOTS)
        if service_key_from_path(compose.relative_to(root).as_posix()) is None
    ]


def service_root_counts(
    composes: list[Path], root: Path = ROOT
) -> dict[str, tuple[int, int]]:
    """Return `{service_root: (compose_count, input_count)}` across `composes`."""
    resolved_root = root.resolve()
    by_root: dict[str, list[Path]] = {sr: [] for sr in _REQUIRED_SERVICE_ROOTS}
    for compose in composes:
        try:
            rel = Path(compose).resolve().relative_to(resolved_root)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] in by_root:
            by_root[rel.parts[0]].append(Path(compose).resolve())
    return {
        sr: (
            len(paths),
            config_hash_input_count(paths, root=resolved_root) if paths else 0,
        )
        for sr, paths in by_root.items()
    }


def dockerfile_canary_violations(composes: list[Path], root: Path = ROOT) -> list[str]:
    """Ensure every deployed service with a `Dockerfile` or `build:` section reads at least one config-hash input (#1123)."""
    if not composes:
        return ["dockerfile canary checked 0 composes; nothing to scan"]
    resolved_root = root.resolve()
    violations: list[str] = []
    by_dir: dict[Path, list[Path]] = {}
    for compose in composes:
        resolved_compose = Path(compose).resolve()
        by_dir.setdefault(resolved_compose.parent, []).append(resolved_compose)
    for service_dir, dir_composes in sorted(by_dir.items()):
        texts = [c.read_text(encoding="utf-8") for c in dir_composes]
        has_dockerfile_or_build = (service_dir / "Dockerfile").is_file() or any(
            _compose_has_build(text) for text in texts
        )
        if not has_dockerfile_or_build:
            continue
        total_inputs = sum(
            len(_compose_artifact_files(str(c), text))
            for c, text in zip(dir_composes, texts, strict=True)
        )
        if total_inputs < 1:
            rel_dir = service_dir.relative_to(resolved_root).as_posix()
            violations.append(
                f"{rel_dir}: has Dockerfile but config hash reads 0 inputs"
            )
    return violations


def host_mount_violations(composes: list[Path], root: Path = ROOT) -> list[str]:
    """Every `${DATA_PATH}/file.ext` host mount needs a hash input and a fingerprint (#1438).

    The service must also read the fingerprint of the rendered file in a label. Without
    it, `docker compose up -d` keeps the running container after a template change.

    The service deploy.py renders such a file from a template in the service directory.
    A template change must change the config hash, or `sync` skips the deploy.
    """
    violations: list[str] = []
    for compose in composes:
        compose = Path(compose).resolve()
        text = compose.read_text(encoding="utf-8")
        rel = compose.relative_to(root.resolve()).as_posix()
        try:
            parse_compose_services(text)
        except ValueError as exc:
            violations.append(f"{rel}: host mount check read nothing: {exc}")
            continue
        inputs = {
            path.name
            for path in _compose_artifact_files(str(compose), text)
            if path.parent == compose.parent
        }
        for name in sorted(set(compose_host_mount_files(text))):
            if name not in inputs and f"{name}.tmpl" not in inputs:
                violations.append(
                    f"{rel}: host mount {name!r} has no config-hash input; "
                    f"add {name!r} or {name + '.tmpl'!r} to the service directory"
                )
        for service, name in compose_mount_fingerprint_gaps(text):
            violations.append(
                f"{rel}: service {service!r} mounts host file {name!r} without a "
                f"fingerprint label; add a label that reads "
                f"${{{mount_fingerprint_var(name)}:-}}"
            )
    return violations


def floor_violations(
    composes: list[Path],
    root: Path = ROOT,
    *,
    counts: dict[str, tuple[int, int]] | None = None,
) -> list[str]:
    """Check unmapped composes, `_SERVICE_ROOTS` completeness, per-root floors, and Dockerfile canary."""
    violations: list[str] = []
    if tuple(_REQUIRED_SERVICE_ROOTS) != _CANONICAL_SERVICE_ROOTS:
        violations.append(
            f"_REQUIRED_SERVICE_ROOTS is missing canonical roots: {list(_REQUIRED_SERVICE_ROOTS)}"
        )
    missing_roots = [sr for sr in _CANONICAL_SERVICE_ROOTS if sr not in _SERVICE_ROOTS]
    if missing_roots:
        violations.append(
            f"_SERVICE_ROOTS is missing required service roots: {', '.join(missing_roots)}"
        )
    missing_floors = [
        sr for sr in _CANONICAL_SERVICE_ROOTS if sr not in _SERVICE_ROOT_FLOORS
    ]
    if missing_floors:
        violations.append(
            f"_SERVICE_ROOT_FLOORS is missing required service roots: {', '.join(missing_floors)}"
        )
    non_positive_floors = [
        sr
        for sr, (min_c, min_i) in _SERVICE_ROOT_FLOORS.items()
        if min_c < 1 or min_i < 1
    ]
    if non_positive_floors:
        violations.append(
            f"_SERVICE_ROOT_FLOORS has non-positive floors: {', '.join(sorted(non_positive_floors))}"
        )
    for rel in find_unmapped_composes(root=root):
        violations.append(
            f"{rel}: compose file does not map to a service key in service_key_from_path"
        )
    resolved_counts = (
        service_root_counts(composes, root=root) if counts is None else counts
    )
    for sr, (min_composes, min_inputs) in _SERVICE_ROOT_FLOORS.items():
        actual_composes, actual_inputs = resolved_counts.get(sr, (0, 0))
        if actual_composes < min_composes:
            violations.append(
                f"{sr}: found {actual_composes} compose files, below floor {min_composes}"
            )
        if actual_inputs < min_inputs:
            violations.append(
                f"{sr}: read {actual_inputs} config-hash inputs, below floor {min_inputs}"
            )
    violations.extend(dockerfile_canary_violations(composes, root=root))
    violations.extend(host_mount_violations(composes, root=root))
    return violations


def _collect_violations(
    composes: list[Path],
    root: Path = ROOT,
    *,
    counts: dict[str, tuple[int, int]] | None = None,
) -> list[str]:
    """Single violation collection pipeline shared by `audit()` and `main()`."""
    try:
        coverage = (
            fanout_coverage_violations(composes)
            if root == ROOT
            else fanout_coverage_violations(composes, root=root)
        )
        structural = floor_violations(composes, root=root, counts=counts)
    except (FileNotFoundError, ValueError) as exc:
        return [str(exc)]
    return sorted(set(coverage) | set(structural))


def audit(root: Path = ROOT) -> list[str]:
    composes = (
        find_service_composes() if root == ROOT else find_service_composes(root=root)
    )
    return _collect_violations(composes, root=root)


def main(root: Path = ROOT) -> int:
    composes = (
        find_service_composes() if root == ROOT else find_service_composes(root=root)
    )
    if not composes:
        # A scan that finds no service reads no input. Reporting "passed" would hide it.
        print("ERROR: no service compose file found; the audit checked nothing.")
        return 1
    try:
        inputs = (
            config_hash_input_count(composes)
            if root == ROOT
            else config_hash_input_count(composes, root=root)
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: invalid compose or Dockerfile config-hash source: {exc}")
        return 1
    if inputs == 0:
        # Compose files that yield no config-hash input leave nothing to check.
        print("ERROR: the compose files yield no config-hash input; checked nothing.")
        return 1
    counts = service_root_counts(composes, root=root)
    all_violations = _collect_violations(composes, root=root, counts=counts)
    breakdown = ", ".join(
        f"{sr}: {c} compose files / {i} inputs" for sr, (c, i) in counts.items()
    )
    if not all_violations:
        print(
            "deploy fan-out coverage audit passed "
            f"({len(composes)} compose files, {inputs} inputs)"
        )
        for sr, (c, i) in counts.items():
            print(f"  {sr}: {c} compose files, {i} inputs")
        return 0

    print("ERROR: config-hash inputs that fan out to no service (under-fan-out risk):")
    for violation in all_violations:
        print(f"  - {violation}")
    print(f"Per-root summary: {breakdown}")
    print(
        "\nFix: add a glob that matches the file to the service's depends_on in "
        "docs/ssot/deploy-dependencies.yaml (e.g. `- common/**` or `- uv.lock`)."
    )
    return 1


REPORT_ENV = "DEPLOY_GUARD_AUDIT_REPORT"
DEFAULT_REPORT_PATH = "deploy-guard-audit-report.json"
DEFAULT_HOOK_RESULT_PATH = "scheduled-audit-result.json"
ALL_GUARD_KEYS = (
    "guard:fanout-sufficiency",
    "guard:webhook-delivery",
    "guard:production-marker-lag",
    "guard:app-hooks-manifest",
    "guard:dokploy-autodeploy",
)
SCHEDULED_AUDIT_STEP_KEY = "guard:scheduled-app-audit"


def report_path(env: Mapping[str, str] | None = None) -> Path:
    current = os.environ if env is None else env
    return Path(current.get(REPORT_ENV) or DEFAULT_REPORT_PATH)


def load_report(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    path = report_path(env)
    if not path.exists():
        return {"checks": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"checks": {}}
    return data if isinstance(data, dict) else {"checks": {}}


def record_check(
    key: str,
    status: str,
    summary: str = "",
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    path = report_path(env)
    report = load_report(env)
    checks = report.setdefault("checks", {})
    if not isinstance(checks, dict):
        checks = {}
        report["checks"] = checks
    checks[key] = {"status": status, "summary": summary}
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")


def run_recorded_check(
    key: str,
    label: str,
    argv: list[str],
    *,
    env: Mapping[str, str] | None = None,
    runner=subprocess.run,
) -> int:
    proc = runner(argv, text=True, capture_output=True, check=False)
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode == 0:
        record_check(key, "pass", label, env=env)
        return 0
    combined = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
    first = combined.splitlines()[-1][:200] if combined else f"exited {proc.returncode}"
    record_check(key, "fail", f"{label}: {first}", env=env)
    return int(proc.returncode)


def _load_hook_results(
    env: Mapping[str, str] | None = None,
    *,
    hook_result_path: Path | None = None,
) -> list[dict[str, Any]] | None:
    target = (
        hook_result_path
        if hook_result_path is not None
        else Path(
            (os.environ if env is None else env).get("SCHEDULED_AUDIT_RESULT")
            or DEFAULT_HOOK_RESULT_PATH
        )
    )
    if not target.exists():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    items = raw.get("scheduled_audit_results") if isinstance(raw, Mapping) else None
    if not isinstance(items, list):
        return None
    return [item for item in items if isinstance(item, dict)]


#: The prefix of every App hook finding key: the one spelling, used by every writer and reader.
APP_HOOK_KEY_PREFIX = "app-hook:"


def app_hook_pair(item: Mapping[str, Any]) -> tuple[str, str]:
    """The ``(identity_key, detail_line)`` of one App hook result: the one spelling of the key."""
    svc = str(item.get("service") or "?")
    slot = str(item.get("slot") or "scheduled_audit")
    norm_env = (
        "production"
        if str(item.get("environment") or "staging") in ("prod", "production")
        else "staging"
    )
    vref = str(item.get("version_ref") or "?")
    raw_summary = (
        str(item.get("summary") or "").strip()
        or str(item.get("stderr_tail") or "").strip()
        or "hook failed"
    )
    summary_line = raw_summary.splitlines()[0][:200]
    return (
        f"{APP_HOOK_KEY_PREFIX}{svc}:{slot}:{norm_env}",
        f"- {svc} [{slot} @ {vref}]: {summary_line}",
    )


def confirmed_finding_pairs(
    env: Mapping[str, str] | None = None,
    *,
    hook_result_path: Path | None = None,
) -> list[tuple[str, str]]:
    """Return ``(identity_key, detail_line)`` pairs for confirmed deploy-guard and App hook failures."""
    report = load_report(env)
    checks = report.get("checks") if isinstance(report.get("checks"), Mapping) else {}
    pairs: list[tuple[str, str]] = []
    for key in ALL_GUARD_KEYS:
        entry = checks.get(key)
        if isinstance(entry, Mapping) and entry.get("status") == "fail":
            summary = str(entry.get("summary") or key).splitlines()[0][:200]
            pairs.append((key, f"- {key}: {summary}"))
    hook_items = _load_hook_results(env, hook_result_path=hook_result_path) or []
    hook_pairs: list[tuple[str, str]] = []
    for item in sorted(hook_items, key=lambda r: str(r.get("service") or "")):
        if item.get("status") == "pass":
            continue
        hook_pairs.append(app_hook_pair(item))
    if hook_pairs:
        pairs.extend(hook_pairs)
    else:
        sched_entry = checks.get(SCHEDULED_AUDIT_STEP_KEY)
        if isinstance(sched_entry, Mapping) and sched_entry.get("status") == "fail":
            summary = str(
                sched_entry.get("summary") or SCHEDULED_AUDIT_STEP_KEY
            ).splitlines()[0][:200]
            pairs.append(
                (SCHEDULED_AUDIT_STEP_KEY, f"- {SCHEDULED_AUDIT_STEP_KEY}: {summary}")
            )
    return pairs


def unevaluated_prefixes(
    env: Mapping[str, str] | None = None,
    *,
    hook_result_path: Path | None = None,
) -> list[str]:
    """Return finding key prefixes that this audit run did not evaluate."""
    report = load_report(env)
    checks = report.get("checks") if isinstance(report.get("checks"), Mapping) else {}
    hook_items = _load_hook_results(env, hook_result_path=hook_result_path)
    if not checks and hook_items is None:
        return [""]
    prefixes = [
        key
        for key in ALL_GUARD_KEYS
        if not isinstance(checks.get(key), Mapping)
        or checks[key].get("status") not in ("pass", "fail")
    ]
    if hook_items is None:
        prefixes.append(APP_HOOK_KEY_PREFIX)
    return prefixes


def observed_all_clean(
    env: Mapping[str, str] | None = None,
    *,
    hook_result_path: Path | None = None,
) -> bool:
    """True when every evaluated check passed and at least one check ran."""
    report = load_report(env)
    checks = report.get("checks") if isinstance(report.get("checks"), Mapping) else {}
    hook_items = _load_hook_results(env, hook_result_path=hook_result_path)
    if not checks and hook_items is None:
        return False
    return not confirmed_finding_pairs(env, hook_result_path=hook_result_path)


def cli(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--skip-check"] and len(args) >= 2:
        reason = args[2] if len(args) >= 3 else "skipped"
        record_check(args[1], "skipped", reason)
        return 0
    if args[:1] == ["--record-check"] and "--" in args:
        sep = args.index("--")
        if sep < 2 or not args[sep + 1 :]:
            raise SystemExit(
                "usage: deploy_guard_audit.py --record-check <key> [label] -- <cmd...>"
            )
        key = args[1]
        label = args[2] if sep >= 3 else key
        return run_recorded_check(key, label, args[sep + 1 :])
    rc = main()
    if os.environ.get(REPORT_ENV):
        record_check(
            "guard:fanout-sufficiency",
            "pass" if rc == 0 else "fail",
            "deploy fan-out coverage sufficiency",
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(cli())
