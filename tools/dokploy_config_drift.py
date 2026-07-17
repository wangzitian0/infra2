#!/usr/bin/env python3
"""Dokploy config-drift detector — is prod running the exact release identity?

The runtime ``IAC_CONFIG_HASH`` remains the deploy idempotence signal and may include secrets.
Drift uses the separately persisted, versioned ``IAC_SOURCE_CONFIG_HASH`` plus
``IAC_DEPLOY_REF``. Both are reconstructible from an immutable release without granting CI
access to runtime secrets.

How the expected hash is computed WITHOUT a worktree (main-worktree only):
  1. Enumerate WHICH files feed a service's hash using the REAL deploy enumeration on disk
     (_compose_artifact_files + the declared dependency globs) — no re-implementation, so the
     file set can't diverge from the deploy.
  2. Read those files' CONTENT at the target ref via ``git cat-file --batch`` (in memory).
  3. Feed (compose, env, items) to libs.deploy.deployer.config_hash_from_items — the SAME pure function
     compute_local_config_hash uses, now path-independent (so a ref's content reproduces the
     iac-runner's hash exactly).

READ-ONLY (git object reads + Dokploy GETs; no deploy, no writes). REPORT-ONLY (exit 0;
``--strict`` exits 1 on real drift for opt-in CI). ``--self-check`` proves the git path matches
disk at HEAD before trusting any tag comparison.
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import glob as _glob  # noqa: E402

from invoke import Context  # noqa: E402

from libs.deploy.deployer import (  # noqa: E402
    Deployer,
    EXACT_COMMIT_RE,
    SOURCE_CONFIG_HASH_VERSION,
    _compose_artifact_files,
    _repo_rel,
    config_hash_from_items,
)
from libs.deploy_dependencies import (  # noqa: E402
    extra_dependency_globs,
    service_key_from_path,
)
from libs import service_registry  # noqa: E402


def _load_deployer(service_id: str) -> type[Deployer] | None:
    layer, name = service_id.split("/", 1)
    base = ROOT / (
        "platform" if layer == "platform" else "finance_report/finance_report"
    )
    deploy_py = next(base.glob(f"*.{name}/deploy.py"), None)
    if deploy_py is None:
        return None
    spec = importlib.util.spec_from_file_location(f"d_{layer}_{name}", deploy_py)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if (
            issubclass(obj, Deployer)
            and obj is not Deployer
            and obj.__module__ == spec.name
        ):
            return obj
    return None


def _hash_input_paths(
    dep: type[Deployer], c: Context
) -> tuple[str, list[str], list[str]]:
    """Repo-relative (compose_path, artifact_paths, dep_paths) for a service, enumerated on disk
    with the REAL deploy enumeration (so the file set matches what the deploy would hash)."""
    compose_content = dep.get_compose_content(c)
    artifact_paths = [
        _repo_rel(p) for p in _compose_artifact_files(dep.compose_path, compose_content)
    ]
    key = service_key_from_path(dep.compose_path)
    matched: set[Path] = set()
    for pattern in extra_dependency_globs(key) if key else []:
        for hit in _glob.glob(str(ROOT / pattern), recursive=True):
            p = Path(hit)
            # Same exclusion as libs.deploy.deployer._dependency_items_from_disk: transient
            # __pycache__/.pyc/.pyo are not in git and absent on a clean checkout.
            if (
                p.is_file()
                and "__pycache__" not in p.parts
                and not p.name.endswith((".pyc", ".pyo"))
            ):
                matched.add(p.resolve())
    dep_paths = [_repo_rel(p) for p in sorted(matched)]
    return dep.compose_path, artifact_paths, dep_paths


def contents_at_ref(ref: str, paths: list[str]) -> dict[str, bytes]:
    """{repo-relative path: content bytes} at a git ref, via one ``git cat-file --batch`` (in
    memory; no checkout / worktree). A path absent at the ref is omitted (caller detects it)."""
    if not paths:
        return {}
    stdin = "".join(f"{ref}:{p}\n" for p in paths).encode()
    proc = subprocess.run(
        ["git", "cat-file", "--batch"], input=stdin, cwd=ROOT, capture_output=True
    )
    proc.check_returncode()
    out: dict[str, bytes] = {}
    buf = proc.stdout
    pos = 0
    for path in paths:
        nl = buf.index(b"\n", pos)
        header = buf[pos:nl].decode()
        pos = nl + 1
        if header.endswith(" missing"):
            continue  # path does not exist at this ref
        _sha, _type, size = header.split()
        size = int(size)
        out[path] = buf[pos : pos + size]
        pos += size + 1  # skip content + trailing newline
    return out


def _source_env_vars(dep: type[Deployer]) -> dict[str, str]:
    """Build the secret-independent env plane used for release identity."""
    e = dep.env()
    return dep.config_env_with_vault_addr(dep.source_config_env_base(e), e)


def expected_hash_at(
    dep: type[Deployer], c: Context, ref: str, env_vars: dict[str, str]
) -> tuple[str | None, list[str]]:
    """(hash, missing_paths) — the config hash recomputed from `ref`'s content. missing_paths
    are hash-input files that don't exist at `ref` (a structural change → can't compare)."""
    compose_path, artifact_paths, dep_paths = _hash_input_paths(dep, c)
    contents = contents_at_ref(ref, [compose_path, *artifact_paths, *dep_paths])
    missing = [
        p for p in (compose_path, *artifact_paths, *dep_paths) if p not in contents
    ]
    if compose_path in missing:
        return None, missing
    compose_content = contents[compose_path].decode("utf-8", "replace")
    artifact_items = [(p, contents[p]) for p in artifact_paths if p in contents]
    dep_items = [(p, contents[p]) for p in dep_paths if p in contents]
    return config_hash_from_items(
        compose_content, env_vars, artifact_items, dep_items
    ), missing


def _set_env(env_name: str) -> None:
    os.environ["ENV"] = env_name
    os.environ["ENV_SUFFIX"] = "" if env_name == "production" else "-staging"
    os.environ["ENV_DOMAIN_SUFFIX"] = "" if env_name == "production" else "-staging"


@dataclass
class Row:
    service: str
    verdict: str
    expected: str | None = None
    deployed: str | None = None
    note: str = ""
    expected_ref: str | None = None
    deployed_ref: str | None = None


@dataclass(frozen=True)
class DeployedIdentity:
    runtime_hash: str | None = None
    source_hash: str | None = None
    deploy_ref: str | None = None


def self_check() -> int:
    """Prove the git-content path reproduces the disk hash at HEAD (clean tree required), so a
    tag comparison can be trusted. Mismatch = the detector's enumeration drifted from deploy."""
    c = Context()
    _set_env("production")
    bad = []
    for sid in service_registry.all_services():
        dep = _load_deployer(sid)
        if dep is None:
            continue
        try:
            ev = _source_env_vars(dep)
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{sid}: source env unavailable: {exc}")
            continue
        try:
            disk = dep.compute_local_config_hash(c, ev)
            via_git, missing = expected_hash_at(dep, c, "HEAD", ev)
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{sid}: ERR {exc}")
            continue
        if missing:
            bad.append(f"{sid}: {len(missing)} input(s) missing at HEAD (dirty tree?)")
        elif disk != via_git:
            bad.append(f"{sid}: disk={disk} via_git={via_git}")
    if bad:
        print("❌ self-check FAILED (git path != disk at HEAD):")
        print("\n".join(f"  {b}" for b in bad))
        return 1
    print(
        "✅ self-check passed: git-content hash == disk hash for every service at HEAD."
    )
    return 0


def _latest_release_tag() -> str:
    tags = subprocess.run(
        ["git", "tag", "--sort=-creatordate"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    for t in tags:
        if t.startswith("v") and t[1:2].isdigit():
            return t
    raise SystemExit("no v* release tag found")


def _commit_at_ref(ref: str) -> str:
    value = subprocess.run(
        ["git", "rev-parse", f"{ref}^{{commit}}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip().lower()
    if not EXACT_COMMIT_RE.fullmatch(value):
        raise RuntimeError(f"{ref} did not resolve to an exact commit SHA")
    return value


def _deployed_identities() -> dict[str, DeployedIdentity]:
    from libs.dokploy import DokployClient

    client = DokployClient()
    out: dict[str, DeployedIdentity] = {}
    for p in client.list_projects():
        if p.get("name") not in ("platform", "finance_report"):
            continue
        for env in p.get("environments", []):
            if env.get("name") != "production":
                continue
            for cp in env.get("compose") or []:
                d = client._request("GET", f"compose.one?composeId={cp['composeId']}")
                env_str = d.get("env") or ""
                values = {}
                for line in env_str.splitlines():
                    key, separator, value = line.partition("=")
                    if separator and key in {
                        "IAC_CONFIG_HASH",
                        "IAC_SOURCE_CONFIG_HASH",
                        "IAC_DEPLOY_REF",
                    }:
                        values[key] = value.strip()
                if values:
                    out[f"{p['name']}/{cp['name']}"] = DeployedIdentity(
                        runtime_hash=values.get("IAC_CONFIG_HASH"),
                        source_hash=values.get("IAC_SOURCE_CONFIG_HASH"),
                        deploy_ref=values.get("IAC_DEPLOY_REF"),
                    )
    return out


def scan(tag: str) -> list[Row]:
    c = Context()
    _set_env("production")
    deployed = _deployed_identities()
    expected_ref = _commit_at_ref(tag)
    rows: list[Row] = []
    for sid in service_registry.all_services():
        dep = _load_deployer(sid)
        if dep is None:
            continue
        identity = deployed.get(sid)
        if identity is None:
            rows.append(Row(sid, "not_deployed"))
            continue
        if (identity.runtime_hash or "").startswith("deploy-"):
            rows.append(
                Row(
                    sid,
                    "cache_bust",
                    deployed=identity.runtime_hash,
                    note="app deploy_v2 path",
                )
            )
            continue
        try:
            env_vars = _source_env_vars(dep)
            exp, missing = expected_hash_at(dep, c, tag, env_vars)
        except Exception as exc:  # noqa: BLE001 — detector failure is not drift
            rows.append(
                Row(
                    sid,
                    "error",
                    deployed=identity.source_hash,
                    note=f"compute error: {exc}",
                    expected_ref=expected_ref,
                    deployed_ref=identity.deploy_ref,
                )
            )
            continue
        versioned_expected = f"{SOURCE_CONFIG_HASH_VERSION}:{exp}" if exp else None
        if missing:
            rows.append(
                Row(
                    sid,
                    "structural",
                    expected=versioned_expected,
                    deployed=identity.source_hash,
                    note=f"{len(missing)} input(s) absent at {tag}",
                    expected_ref=expected_ref,
                    deployed_ref=identity.deploy_ref,
                )
            )
            continue
        if not identity.source_hash or not identity.deploy_ref:
            legacy_note = "source identity not deployed yet"
            if identity.runtime_hash == exp:
                legacy_note += "; legacy runtime hash matches release source"
            rows.append(
                Row(
                    sid,
                    "legacy_identity",
                    expected=versioned_expected,
                    deployed=identity.source_hash,
                    note=legacy_note,
                    expected_ref=expected_ref,
                    deployed_ref=identity.deploy_ref,
                )
            )
            continue
        if not identity.source_hash.startswith(f"{SOURCE_CONFIG_HASH_VERSION}:"):
            rows.append(
                Row(
                    sid,
                    "error",
                    expected=versioned_expected,
                    deployed=identity.source_hash,
                    note="unsupported source config hash version",
                    expected_ref=expected_ref,
                    deployed_ref=identity.deploy_ref,
                )
            )
            continue
        if not EXACT_COMMIT_RE.fullmatch(identity.deploy_ref):
            rows.append(
                Row(
                    sid,
                    "error",
                    expected=versioned_expected,
                    deployed=identity.source_hash,
                    note="deployed ref is not an exact commit SHA",
                    expected_ref=expected_ref,
                    deployed_ref=identity.deploy_ref,
                )
            )
            continue
        try:
            provenance_hash, provenance_missing = expected_hash_at(
                dep, c, identity.deploy_ref, env_vars
            )
        except Exception as exc:  # noqa: BLE001
            rows.append(
                Row(
                    sid,
                    "error",
                    expected=versioned_expected,
                    deployed=identity.source_hash,
                    note=f"could not verify deployed ref provenance: {exc}",
                    expected_ref=expected_ref,
                    deployed_ref=identity.deploy_ref,
                )
            )
            continue
        provenance_identity = (
            f"{SOURCE_CONFIG_HASH_VERSION}:{provenance_hash}"
            if provenance_hash
            else None
        )
        if provenance_missing or provenance_identity != identity.source_hash:
            rows.append(
                Row(
                    sid,
                    "DRIFT",
                    expected=versioned_expected,
                    deployed=identity.source_hash,
                    note="stored source identity does not match its deployed ref",
                    expected_ref=expected_ref,
                    deployed_ref=identity.deploy_ref,
                )
            )
            continue
        if identity.source_hash == versioned_expected:
            rows.append(
                Row(
                    sid,
                    "in_sync",
                    versioned_expected,
                    identity.source_hash,
                    expected_ref=expected_ref,
                    deployed_ref=identity.deploy_ref,
                )
            )
        else:
            rows.append(
                Row(
                    sid,
                    "DRIFT",
                    versioned_expected,
                    identity.source_hash,
                    expected_ref=expected_ref,
                    deployed_ref=identity.deploy_ref,
                )
            )
    return rows


def format_report(tag: str, rows: list[Row]) -> str:
    drift = [r for r in rows if r.verdict == "DRIFT"]
    n = lambda v: sum(1 for r in rows if r.verdict == v)  # noqa: E731
    errors = [r for r in rows if r.verdict == "error"]
    structural = [r for r in rows if r.verdict == "structural"]
    lines = [
        f"📋 [Infra2] config-drift · production vs release {tag}",
        f"in sync {n('in_sync')} · DRIFT {len(drift)} · error {len(errors)} · "
        f"not deployed {n('not_deployed')} · structural {n('structural')} · "
        f"legacy {n('legacy_identity')} · n/a(app) {n('cache_bust')}",
    ]
    for r in drift:
        lines.append(
            f"🔴 DRIFT {r.service}: deployed≠release "
            f"(expected={r.expected}@{r.expected_ref} "
            f"deployed={r.deployed}@{r.deployed_ref})"
        )
    for r in errors:
        # Surface tool/runtime failures loudly — a silently-skipped service must not let the run
        # look healthy (a "0 drift" that actually never checked N services is the lie to avoid).
        lines.append(f"⚠️ ERROR {r.service}: could not compute expected hash ({r.note})")
    for r in rows:
        if r.verdict == "not_deployed":
            lines.append(f"  · not deployed: {r.service}")
        elif r.verdict == "structural":
            lines.append(f"  · structural: {r.service} ({r.note})")
        elif r.verdict == "env_unavailable":
            lines.append(f"  · env-skip: {r.service} (needs op to recompute)")
        elif r.verdict == "legacy_identity":
            lines.append(f"  · legacy identity: {r.service} ({r.note})")
    if n("in_sync") and not drift and not errors and not structural:
        lines.append(f"✅ every comparable service matches release {tag}.")
    return "\n".join(lines)


def strict_blockers(rows: list[Row]) -> list[Row]:
    """Rows that make a strict drift run fail closed."""
    return [r for r in rows if r.verdict in {"DRIFT", "error", "structural"}]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--self-check", action="store_true", help="prove git path == disk at HEAD"
    )
    parser.add_argument(
        "--strict", action="store_true", help="exit 1 if any real DRIFT"
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="post the report to the infra2 reports Lark group",
    )
    args = parser.parse_args()

    if args.self_check:
        return self_check()

    tag = _latest_release_tag()
    rows = scan(tag)
    report = format_report(tag, rows)
    print(report)

    if args.report:
        from libs.alerting import deliver_infra2_report

        # Post daily even when in-sync: the report's own delivery self-proves the path (#425),
        # so a broken reconciler is distinguishable from "no drift".
        delivered = deliver_infra2_report(report)
        print(f"\n[report delivered: {delivered}]", file=sys.stderr)

    return 1 if (args.strict and strict_blockers(rows)) else 0


if __name__ == "__main__":
    sys.exit(main())
