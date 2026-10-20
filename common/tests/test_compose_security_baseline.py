"""Contract tests for #724-R3: compose security baseline and CI Trivy scan."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = REPO_ROOT / "docs/ssot/compose-security-baseline.json"
TRIVY_WORKFLOW_PATH = REPO_ROOT / ".github/workflows/trivy-scan.yml"


def _git_tracked_compose_files() -> list[Path]:
    proc = subprocess.run(
        [
            "git",
            "ls-files",
            "**/compose*.yaml",
            "**/compose*.yml",
            "compose*.yaml",
            "compose*.yml",
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    paths: list[Path] = []
    for line in proc.stdout.splitlines():
        rel = line.strip()
        if not rel or rel.startswith("repos/") or rel.startswith("oh-my-code-agent/"):
            continue
        p = REPO_ROOT / rel
        if p.exists():
            paths.append(p)
    return sorted(set(paths))


def is_non_root_user(user_val: Any) -> bool:
    """Return True if a compose service `user:` value runs as non-root."""
    if user_val is None:
        return False
    text = str(user_val).strip()
    if not text:
        return False
    uid_part = text.split(":", 1)[0].strip()
    if uid_part in {"0", "root"}:
        return False
    return True


def has_cap_drop_all(cap_drop_val: Any) -> bool:
    """Return True if a compose service `cap_drop:` list includes ALL."""
    if not isinstance(cap_drop_val, list):
        return False
    normalized = {str(item).strip().upper() for item in cap_drop_val}
    return "ALL" in normalized


def scan_compose_security_gaps(
    compose_files: list[Path], root: Path = REPO_ROOT
) -> dict[str, list[str]]:
    """Scan compose files and return services missing non-root user or cap_drop: ALL."""
    missing_non_root: list[str] = []
    missing_cap_drop_all: list[str] = []

    for path in sorted(compose_files):
        rel = path.relative_to(root).as_posix()
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        services = data.get("services") or {}
        if not isinstance(services, dict):
            continue
        for svc_name in sorted(services):
            svc = services[svc_name] or {}
            if not isinstance(svc, dict):
                continue
            key = f"{rel}::{svc_name}"
            if not is_non_root_user(svc.get("user")):
                missing_non_root.append(key)
            if not has_cap_drop_all(svc.get("cap_drop")):
                missing_cap_drop_all.append(key)

    return {
        "missing_non_root": sorted(missing_non_root),
        "missing_cap_drop_all": sorted(missing_cap_drop_all),
    }


def evaluate_baseline_ratchet(
    actual: dict[str, list[str]], baseline: dict[str, list[str]]
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Return (new_regressions, stale_baseline_entries) across both dimensions."""
    regressions: dict[str, list[str]] = {}
    stale: dict[str, list[str]] = {}
    for key in ("missing_non_root", "missing_cap_drop_all"):
        actual_set = set(actual.get(key, []))
        base_set = set(baseline.get(key, []))
        added = sorted(actual_set - base_set)
        removed = sorted(base_set - actual_set)
        if added:
            regressions[key] = added
        if removed:
            stale[key] = removed
    return regressions, stale


def test_compose_security_baseline_ratchet_matches_repo() -> None:
    """All tracked compose services must either satisfy non-root + cap_drop:ALL or be in the shrinking ratchet."""
    assert BASELINE_PATH.exists(), f"Missing baseline file: {BASELINE_PATH}"
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    actual = scan_compose_security_gaps(_git_tracked_compose_files())

    regressions, stale = evaluate_baseline_ratchet(actual, baseline)
    assert regressions == {}, (
        f"New compose services are missing non-root user or cap_drop: [ALL] (#724-R3): {regressions}"
    )
    assert stale == {}, (
        f"Compose services were hardened; shrink docs/ssot/compose-security-baseline.json to lock in the gain: {stale}"
    )


def test_portal_compose_enforces_non_root_and_cap_drop_all() -> None:
    """platform/21.portal/compose.yaml::portal is the reference hardened service."""
    portal_compose = REPO_ROOT / "platform/21.portal/compose.yaml"
    gaps = scan_compose_security_gaps([portal_compose])
    assert gaps["missing_non_root"] == []
    assert gaps["missing_cap_drop_all"] == []


def test_compose_security_ratchet_detects_mutations(tmp_path: Path) -> None:
    """Falsifiability check: adding an unhardened service or hardening an existing one triggers a failure."""
    compose_file = tmp_path / "platform/demo/compose.yaml"
    compose_file.parent.mkdir(parents=True, exist_ok=True)
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "hardened": {
                        "image": "nginx:alpine",
                        "user": "101:101",
                        "cap_drop": ["ALL"],
                    },
                    "legacy_root": {"image": "postgres:15", "user": "0:0"},
                }
            }
        ),
        encoding="utf-8",
    )
    actual = scan_compose_security_gaps([compose_file], root=tmp_path)
    assert actual == {
        "missing_non_root": ["platform/demo/compose.yaml::legacy_root"],
        "missing_cap_drop_all": ["platform/demo/compose.yaml::legacy_root"],
    }

    empty_baseline = {"missing_non_root": [], "missing_cap_drop_all": []}
    regressions, stale = evaluate_baseline_ratchet(actual, empty_baseline)
    assert regressions == {
        "missing_non_root": ["platform/demo/compose.yaml::legacy_root"],
        "missing_cap_drop_all": ["platform/demo/compose.yaml::legacy_root"],
    }
    assert stale == {}

    inflated_baseline = {
        "missing_non_root": [
            "platform/demo/compose.yaml::hardened",
            "platform/demo/compose.yaml::legacy_root",
        ],
        "missing_cap_drop_all": ["platform/demo/compose.yaml::legacy_root"],
    }
    regressions2, stale2 = evaluate_baseline_ratchet(actual, inflated_baseline)
    assert regressions2 == {}
    assert stale2 == {"missing_non_root": ["platform/demo/compose.yaml::hardened"]}


def test_trivy_workflow_scans_vulnerabilities_and_misconfigs() -> None:
    """CI Trivy scan workflow must exist and scan vuln,misconfig for CRITICAL,HIGH (#724-R3)."""
    assert TRIVY_WORKFLOW_PATH.exists(), f"Missing {TRIVY_WORKFLOW_PATH}"
    wf = yaml.safe_load(TRIVY_WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert wf["permissions"] == {"contents": "read"}
    jobs = wf.get("jobs") or {}
    assert "trivy-fs" in jobs
    steps = jobs["trivy-fs"].get("steps") or []
    trivy_steps = [
        s for s in steps if "aquasecurity/trivy-action" in str(s.get("uses", ""))
    ]
    assert len(trivy_steps) == 1
    with_args = trivy_steps[0].get("with") or {}
    assert with_args.get("scan-type") == "fs"
    assert "vuln" in str(with_args.get("scanners", ""))
    assert "misconfig" in str(with_args.get("scanners", ""))
    assert "CRITICAL" in str(with_args.get("severity", ""))
    assert "HIGH" in str(with_args.get("severity", ""))
