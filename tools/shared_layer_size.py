#!/usr/bin/env python3
"""Shared-layer size telemetry across the 4-repo workspace (#193).

Report-only probe: per-scope raw and zlib bytes for non-test Python, exact
duplicate clusters (sha256), and same-basename near-duplicate pairs across
scopes (line-overlap similarity). Exclusion is a report, never a gate: the
exit code is 0 whenever the inputs are readable.

Scopes follow #193: the SDK package, the infra2 shared layer (common/ +
tools/), and each app checkout. Any missing scope reports as absent instead
of failing, so the probe runs on partial checkouts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zlib
from pathlib import Path

EXCLUDE_DIRS = {
    "__pycache__",
    "node_modules",
    ".venv",
    ".git",
    "static",
    "dist",
    "migrations",
    "fixtures",
    "cassettes",
}
NEARDUP_SIMILARITY_BAR = 0.30
NEARDUP_MIN_BYTES = 2000

SCOPES: dict[str, list[str]] = {
    "sdk": ["repos/infra2-sdk/src/infra2_sdk"],
    "infra2": ["common", "tools"],
    "finance_report": ["repos/finance_report"],
    "truealpha": ["repos/truealpha"],
}


def _is_test(path: str) -> bool:
    name = os.path.basename(path)
    if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py":
        return True
    return f"{os.sep}tests{os.sep}" in path


def collect(root: Path, dirs: list[str]) -> list[Path]:
    out: list[Path] = []
    for rel in dirs:
        base = root / rel
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [
                d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith(".")
            ]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                p = Path(dirpath) / fn
                if _is_test(str(p)):
                    continue
                out.append(p)
    return sorted(out)


def _scope_of(path: Path, root: Path) -> str:
    rel = path.relative_to(root).as_posix()
    for scope, dirs in SCOPES.items():
        for d in dirs:
            if rel == d or rel.startswith(d + "/"):
                return scope
    return "?"


def measure(root: Path, top_n: int) -> dict:
    scopes: dict[str, dict] = {}
    contents: dict[Path, bytes] = {}
    hashes: dict[Path, str] = {}
    zsizes: dict[Path, int] = {}
    for scope, dirs in SCOPES.items():
        files = collect(root, dirs)
        raw = z = 0
        for p in files:
            b = p.read_bytes()
            contents[p] = b
            hashes[p] = hashlib.sha256(b).hexdigest()
            zsizes[p] = len(zlib.compress(b, 9))
            raw += len(b)
            z += zsizes[p]
        scopes[scope] = {
            "dirs": dirs,
            "present": bool(files) or all((root / d).is_dir() for d in dirs),
            "files": len(files),
            "raw_bytes": raw,
            "zlib_bytes": z,
        }

    by_hash: dict[str, list[Path]] = {}
    for p, h in hashes.items():
        by_hash.setdefault(h, []).append(p)
    duplicates = []
    for h, paths in sorted(by_hash.items(), key=lambda kv: -len(contents[kv[1][0]])):
        if len(paths) < 2:
            continue
        size = len(contents[paths[0]])
        duplicates.append(
            {
                "size_bytes": size,
                "waste_bytes": size * (len(paths) - 1),
                "paths": sorted(str(p.relative_to(root)) for p in paths),
            }
        )
    duplicates.sort(key=lambda d: -d["waste_bytes"])

    by_base: dict[str, list[Path]] = {}
    for p in contents:
        by_base.setdefault(p.name, []).append(p)
    lines = {
        p: {ln.strip() for ln in b.splitlines() if ln.strip()}
        for p, b in contents.items()
        if len(b) > NEARDUP_MIN_BYTES
    }
    near_dups = []
    for base, paths in by_base.items():
        big = [p for p in paths if p in lines]
        if len(big) < 2:
            continue
        for i, a in enumerate(big):
            for b in big[i + 1 :]:
                sa, sb = _scope_of(a, root), _scope_of(b, root)
                if sa == sb or "?" in (sa, sb):
                    continue
                union = len(lines[a] | lines[b])
                sim = (len(lines[a] & lines[b]) * 2) / union if union else 0.0
                if sim >= NEARDUP_SIMILARITY_BAR:
                    near_dups.append(
                        {
                            "similarity": round(sim, 3),
                            "a": str(a.relative_to(root)),
                            "b": str(b.relative_to(root)),
                            "raw_bytes": len(contents[a]) + len(contents[b]),
                        }
                    )
    near_dups.sort(key=lambda d: -d["similarity"])

    return {
        "scopes": scopes,
        "exact_duplicate_clusters": duplicates,
        "identical_copy_waste_bytes": sum(d["waste_bytes"] for d in duplicates),
        "near_duplicates_top": near_dups[:top_n],
        "near_duplicate_count": len(near_dups),
        "similarity_bar": NEARDUP_SIMILARITY_BAR,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="workspace root (default: cwd)")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    parser.add_argument("--top", type=int, default=14, help="near-duplicate list size")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    report = measure(root, args.top)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"{'scope':<16} {'files':>6} {'raw KB':>9} {'zlib KB':>9}")
        for scope, s in report["scopes"].items():
            if not s["present"] and s["files"] == 0:
                print(f"{scope:<16} {'ABSENT':>6}")
                continue
            print(
                f"{scope:<16} {s['files']:>6} {s['raw_bytes'] / 1024:>9.1f}"
                f" {s['zlib_bytes'] / 1024:>9.1f}"
            )
        print(
            f"exact duplicate clusters: {len(report['exact_duplicate_clusters'])}"
            f" (waste {report['identical_copy_waste_bytes'] / 1024:.1f} KB);"
            f" near duplicates >= {report['similarity_bar']:.0%}: "
            f"{report['near_duplicate_count']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
