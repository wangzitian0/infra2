"""Contract tests for #724-R4: supply chain image digest pins, lockfile hashes, and automerge guard."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from tools.sdk_requirement import sdk_requirement

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = REPO_ROOT / "docs/ssot/supply-chain-baseline.json"
SHA256_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
FROM_LINE_RE = re.compile(
    r"^\s*FROM\s+(?:--platform=\S+\s+)?(?P<image>\S+)(?:\s+AS\s+(?P<stage>\S+))?\s*$",
    re.IGNORECASE,
)


def _git_tracked_files(*patterns: str) -> list[Path]:
    proc = subprocess.run(
        ["git", "ls-files", *patterns],
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


def is_digest_pinned(image_ref: str) -> bool:
    """Return True if an image reference ends with `@sha256:<64 hex chars>`."""
    ref = str(image_ref).strip()
    return bool(SHA256_DIGEST_RE.search(ref))


def scan_supply_chain_unpinned(
    compose_files: list[Path], dockerfiles: list[Path], root: Path = REPO_ROOT
) -> dict[str, list[str]]:
    """Return unpinned compose service images and Dockerfile FROM references."""
    unpinned_compose: list[str] = []
    unpinned_dockerfiles: list[str] = []

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
            image = svc.get("image")
            if image is None or "build" in svc:
                continue
            if not is_digest_pinned(str(image)):
                unpinned_compose.append(f"{rel}::{svc_name}")

    for path in sorted(dockerfiles):
        rel = path.relative_to(root).as_posix()
        stages: set[str] = set()
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            m = FROM_LINE_RE.match(line)
            if not m:
                continue
            image = m.group("image")
            stage = m.group("stage")
            if stage:
                stages.add(stage)
            if image.lower() == "scratch" or image in stages:
                continue
            if not is_digest_pinned(image):
                unpinned_dockerfiles.append(f"{rel}::{image}")

    return {
        "unpinned_compose_images": sorted(unpinned_compose),
        "unpinned_dockerfile_froms": sorted(unpinned_dockerfiles),
    }


def evaluate_supply_chain_ratchet(
    actual: dict[str, list[str]], baseline: dict[str, list[str]]
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Return (new_regressions, stale_baseline_entries) across supply-chain pin lists."""
    regressions: dict[str, list[str]] = {}
    stale: dict[str, list[str]] = {}
    for key in ("unpinned_compose_images", "unpinned_dockerfile_froms"):
        actual_set = set(actual.get(key, []))
        base_set = set(baseline.get(key, []))
        added = sorted(actual_set - base_set)
        removed = sorted(base_set - actual_set)
        if added:
            regressions[key] = added
        if removed:
            stale[key] = removed
    return regressions, stale


def find_automerge_violations(config_obj: Any, path_prefix: str = "") -> list[str]:
    """Recursively scan a parsed Renovate/Dependabot config object for automerge=True."""
    violations: list[str] = []
    if isinstance(config_obj, dict):
        for k, v in config_obj.items():
            cur = f"{path_prefix}.{k}" if path_prefix else str(k)
            if k in {"automerge", "platformAutomerge"} and v is True:
                violations.append(f"{cur}=true")
            violations.extend(find_automerge_violations(v, cur))
    elif isinstance(config_obj, list):
        for idx, item in enumerate(config_obj):
            cur = f"{path_prefix}[{idx}]"
            violations.extend(find_automerge_violations(item, cur))
    return violations


def test_supply_chain_image_digest_ratchet_matches_repo() -> None:
    """All tracked compose images and Dockerfile FROM lines must be digest-pinned or in the shrinking ratchet."""
    assert BASELINE_PATH.exists(), f"Missing baseline file: {BASELINE_PATH}"
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    compose_files = _git_tracked_files(
        "**/compose*.yaml", "**/compose*.yml", "compose*.yaml", "compose*.yml"
    )
    dockerfiles = _git_tracked_files("**/Dockerfile*", "Dockerfile*")
    actual = scan_supply_chain_unpinned(compose_files, dockerfiles)

    regressions, stale = evaluate_supply_chain_ratchet(actual, baseline)
    assert regressions == {}, (
        f"New unpinned image tag introduced without @sha256 digest (#724-R4): {regressions}"
    )
    assert stale == {}, (
        f"Images were digest-pinned; shrink docs/ssot/supply-chain-baseline.json: {stale}"
    )


def test_portal_image_is_pinned_by_sha256_digest() -> None:
    """platform/21.portal/compose.yaml::portal is pinned by @sha256 digest."""
    portal = REPO_ROOT / "platform/21.portal/compose.yaml"
    res = scan_supply_chain_unpinned([portal], [])
    assert res["unpinned_compose_images"] == []


def test_python_lockfile_and_dockerfiles_enforce_sha256_hashes() -> None:
    """uv.lock must pin infra2-sdk with a sha256 hash and Dockerfiles installing sdk must use sdk_requirement.py."""
    lock_path = REPO_ROOT / "uv.lock"
    req = sdk_requirement(lock_path)
    assert "#sha256=" in req

    sdk_dockerfiles = [
        REPO_ROOT / "bootstrap/06.iac_runner/Dockerfile",
        REPO_ROOT / "platform/12.alerting/Dockerfile",
        REPO_ROOT / "platform/30.todo/Dockerfile",
    ]
    for df in sdk_dockerfiles:
        text = df.read_text(encoding="utf-8")
        assert "tools/sdk_requirement.py" in text, (
            f"{df} must resolve infra2-sdk hash via tools/sdk_requirement.py"
        )


def test_dependency_bot_configs_forbid_automerge() -> None:
    """Any Renovate or Dependabot configuration in the repository must keep automerge disabled (#724-R4)."""
    bot_files = _git_tracked_files(
        ".github/dependabot.yml",
        ".github/dependabot.yaml",
        "renovate.json",
        ".github/renovate.json",
        ".renovaterc",
        ".renovaterc.json",
    )
    for path in bot_files:
        raw = path.read_text(encoding="utf-8")
        parsed = (
            json.loads(raw)
            if path.suffix == ".json" or path.name.startswith(".renovaterc")
            else yaml.safe_load(raw)
        )
        violations = find_automerge_violations(parsed)
        assert violations == [], f"{path} enables automerge (#724-R4): {violations}"

    # Falsifiability check on synthetic Renovate configs
    bad_config = {
        "extends": ["config:recommended"],
        "automerge": False,
        "packageRules": [{"matchUpdateTypes": ["minor", "patch"], "automerge": True}],
    }
    assert find_automerge_violations(bad_config) == ["packageRules[0].automerge=true"]
    good_config = {
        "extends": ["config:recommended"],
        "automerge": False,
        "packageRules": [{"matchUpdateTypes": ["minor", "patch"], "automerge": False}],
    }
    assert find_automerge_violations(good_config) == []


def test_supply_chain_ratchet_detects_mutations(tmp_path: Path) -> None:
    """Falsifiability check: unpinned compose image or Dockerfile FROM triggers regression."""
    comp = tmp_path / "svc/compose.yaml"
    comp.parent.mkdir(parents=True, exist_ok=True)
    comp.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "pinned": {
                        "image": "alpine:3.20@sha256:" + ("a" * 64),
                    },
                    "unpinned": {
                        "image": "alpine:3.20",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    df = tmp_path / "svc/Dockerfile"
    df.write_text(
        "FROM python:3.11-slim@" + "sha256:" + ("b" * 64) + " AS builder\n"
        "FROM builder AS stage2\n"
        "FROM ubuntu:24.04\n",
        encoding="utf-8",
    )
    actual = scan_supply_chain_unpinned([comp], [df], root=tmp_path)
    assert actual == {
        "unpinned_compose_images": ["svc/compose.yaml::unpinned"],
        "unpinned_dockerfile_froms": ["svc/Dockerfile::ubuntu:24.04"],
    }
    reg, stale = evaluate_supply_chain_ratchet(
        actual, {"unpinned_compose_images": [], "unpinned_dockerfile_froms": []}
    )
    assert reg == actual
    assert stale == {}
